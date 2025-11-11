#!/usr/bin/env python3
r"""
AI Terminal Shim — PTY-based terminal with LangGraph integration
- Spawns a real bash under a PTY
- Properly relays keystrokes and output
- Handles window resize and Ctrl-C/Z/\ signals
- Intercepts completed input lines for AI transformation
- Integrates with existing LangGraph nodes for command processing

Slash-commands:
  /help            Show commands
  /e <text>        Turn natural language into a shell command
  /repair on       Enable auto-repair with interactive approval
  /repair auto     Enable auto-repair and auto-run fixes
  /repair off      Disable auto-repair
  /dry             Enable dry-run mode (same as /dry on)
  /dry on          Enable dry-run mode
  /dry off         Disable dry-run mode
  /dry status      Show current dry-run status
  /alias basic     Enable ll/la/l aliases
  /quit            Exit
"""

# ---------------------------------------------------------------------
# Early env tweaks to quiet noisy libs (absl/grpc/tf). These must be
# set before importing providers that may emit boot logs.
# ---------------------------------------------------------------------
import os
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "3")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

import pty
import sys
import tty
import termios
import fcntl
import struct
import signal
import select
from typing import Optional, Dict, Tuple, List
import re

# Optional: Rich (UI polish). We fall back cleanly if unavailable.
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.align import Align
    from rich.text import Text
    from rich.console import Group
    _HAS_RICH = True
except Exception:
    _HAS_RICH = False

# Project imports
from .graph import build_graph  # kept for compatibility (may be used by callers)
from .state import State
from .safety import check_danger
from .nodes import (
    replan as llm_replan,
    from_english as llm_from_english,
    from_error as llm_from_error,
)
from .llm import get_llm
from .ux import label, header, code, warn, error as color_error, success, dim, progress

# ----------------------------
# Configuration / Filters
# ----------------------------

BASH = ["bash", "--noprofile", "--norc"]   # Use ["bash", "-l"] to load rc files

STATUS_INLINE = "[[AI:STATUS:"
STATUS_LINE_RE = re.compile(r"^\s*\[\[AI:STATUS:(-?\d+)\]\]\s*$")

# Suppress these noisy absl/gRPC lines that can appear before PTY starts
SUPPRESS_SUBSTRINGS = [
    "WARNING: All log messages before absl::InitializeLog() is called are written to STDERR",
    "alts_credentials.cc:93",
    "ALTS creds ignored",
]

class _FilteredStderr:
    """Filter absl/gRPC boot warnings from *our process* (before we start the PTY)."""
    def __init__(self, wrapped, substrings):
        self._w = wrapped
        self._buf = ""
        self._subs = tuple(substrings)

    def write(self, s):
        self._buf += str(s)
        # Only flush at newlines; we want to filter full lines
        if "\n" not in self._buf:
            return
        parts = self._buf.splitlines(keepends=True)
        tail = "" if parts[-1].endswith("\n") else parts.pop()
        for p in parts:
            if any(x in p for x in self._subs):
                continue
            self._w.write(p)
        self._buf = tail

    def flush(self):
        if self._buf and not any(x in self._buf for x in self._subs):
            self._w.write(self._buf)
        self._buf = ""
        try:
            self._w.flush()
        except Exception:
            pass

# Install the stderr filter immediately (safe to rewrap)
sys.stderr = _FilteredStderr(sys.stderr, SUPPRESS_SUBSTRINGS)

# ----------------------------
# Banner
# ----------------------------

if _HAS_RICH:
    # Center only the product name; keep body left-aligned for clarity
    title = Text.from_markup("[bold cyan]BashBard[/bold cyan]")
    title.justify = "center"

    body = Text.from_markup(
        "[white]AI-Powered Command Intelligence for Secure Linux Operations[/white]\n"
        "[white]Transforms natural language and errors into safe, auditable shell commands[/white]\n"
        "[white]Understands intent • Repairs commands • Enforces safety by design[/white]\n\n"
        "[green]Organization:[/] Cyber Force LLC\n"
        "[green]Authors:[/] Khafagy & Elnagaar\n"
        "[green]Core Strengths:[/] Intelligence • Safety • Explainability • Automation\n"
        "[green]Mission:[/] Empower professionals to work smarter and safer through AI-driven command insight\n"
        "[green]License:[/] Apache-2.0 license"
    )

    content = Group(Align.center(title), body)  # ← true center for just "BashBard"

    BANNER = Panel(
        Align.center(content, vertical="middle"),
        border_style="bright_blue",
        padding=(1, 4),
        title="[bold bright_blue]Cyber Force LLC[/bold bright_blue]",
        subtitle="[italic bright_cyan]Secure • Intelligent • Human-Centered Automation[/italic bright_cyan]",
    )
else:
    BANNER = None  # Fallback if Rich not available

def _print_banner() -> None:
    """Render the startup banner with Rich if available; plain text otherwise."""
    try:
        if _HAS_RICH and BANNER is not None:
            Console().print(BANNER)
        else:
            print(
                "\n"
                "======================= BashBard =======================\n"
                "AI-Powered Command Intelligence for Secure Linux Operations\n"
                "Transforms natural language and errors into safe, auditable shell commands\n"
                "Understands intent • Repairs commands • Enforces safety by design\n"
                "Organization: Cyber Force LLC | Authors: Khafagy & Nagaar\n"
                "Core Strengths: Intelligence • Safety • Explainability • Automation\n"
                "Mission: Empower professionals to work smarter and safer through AI-driven command insight\n"
                "License: Apache-2.0 license\n"
                "========================================================\n"
            )
    except Exception:
        # Never let banner printing kill the TTY
        pass

# ----------------------------
# LLM helpers
# ----------------------------

def _sanitize_llm_command_text(text: str) -> Optional[str]:
    """Extract a single-line shell command from model output."""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    # tolerate JSON-y responses and extract "command"
    if '"command"' in candidate or candidate.startswith("{"):
        m = re.search(r'"command"\s*:\s*"(.*?)"', candidate, re.DOTALL)
        if m:
            extracted = m.group(1).replace("\r", " ").replace("\n", " ").strip()
            return extracted or None
        return None
    # hard-stop on multi-line
    if "\n" in candidate or "\r" in candidate:
        return None
    return candidate or None

def english_to_command_if_needed(user_line: str, *, cwd: str, env: dict) -> str:
    """Convert natural language to a command (no execution), printing model rationale if provided."""
    try:
        state: State = {"user_request": user_line, "dry_run": False, "quiet": True, "strict_json": True}
        result = llm_from_english(state) or {}
        raw_cmd = result.get("candidate_command", "") or ""
        mode = (result.get("candidate_mode") or "run").lower()
        if raw_cmd and mode != "explain":
            cmd = _sanitize_llm_command_text(raw_cmd)
            if not cmd:
                print(f"\r\n{label('AI')} Command extraction failed; model returned non-shell content.\r\n", end="")
                return ""
            if result.get("candidate_explanation"):
                print(f"\r\n{label('AI')} {dim(result['candidate_explanation'])}\r\n", end="")
            return cmd
        elif result.get("candidate_explanation"):
            print(f"\r\n{label('AI')} {dim(result['candidate_explanation'])}\r\n", end="")
            return ""
        return ""
    except Exception as e:
        print(f"\r\n{label('AI','warning')} {color_error(str(e))}\r\n", end="")
        return ""

def repair_command_if_needed(prev_cmd: str, last_output_chunk: str, *, cwd: str, env: dict) -> Optional[str]:
    """Ask the model to repair a failing command based on stderr/stdout context."""
    try:
        state: State = {
            "last_command": prev_cmd,
            "last_error": last_output_chunk,
            "dry_run": False,
            "quiet": True,
            "strict_json": True,
        }
        result = llm_from_error(state) or {}
        raw_cmd = result.get("candidate_command", "") or ""
        mode = (result.get("candidate_mode") or "run").lower()
        if raw_cmd and mode != "explain":
            cmd = _sanitize_llm_command_text(raw_cmd)
            if not cmd:
                print(f"\r\n{label('AI Repair','warning')} Command extraction failed; model returned non-shell content.\r\n", end="")
                return None
            if result.get("candidate_explanation"):
                print(f"\r\n{label('AI Repair')} {dim(result['candidate_explanation'])}\r\n", end="")
            return cmd
        elif result.get("candidate_explanation"):
            print(f"\r\n{label('AI')} {dim(result['candidate_explanation'])}\r\n", end="")
        return None
    except Exception as e:
        print(f"\r\n{label('AI Repair','warning')} {color_error(str(e))}\r\n", end="")
        return None

def approval_gate(cmd: str, *, context: str) -> bool:
    """
    Risk check + human approval using existing danger check.
    Always left-aligned; provides concise expert guidance with reasons.
    """
    try:
        danger_result = check_danger(cmd) or {}
        is_dangerous = bool(danger_result.get("danger", False))
        reasons = danger_result.get("reasons", []) or []

        if is_dangerous:
            print(f"\r\n{header('DANGEROUS COMMAND','danger')}\r\n{code('$ ' + cmd)}\r\n", end="")
            if reasons:
                print(warn("Why this is risky:") + "\r\n", end="")
                for r in reasons:
                    print(f" - {r}\r\n", end="")
            print(dim(
                "Best practices: run read-only first (e.g., --dry-run/--check), "
                "verify paths, and ensure backups/permissions are correct.\r\n"
            ), end="")
            # Ask for confirmation
            print(f"{warn('Proceed?')} {dim('[y/N]')}: ", end="")
            sys.stdout.flush()
            response = ""
            while True:
                ch = os.read(sys.stdin.fileno(), 1)
                if ch in (b"\r", b"\n"):
                    print("\r\n", end="")
                    break
                if ch == b"\x03":  # Ctrl-C
                    print("\r\n", end="")
                    return False
                response += ch.decode("utf-8", "ignore")
                os.write(sys.stdout.fileno(), ch)
            return response.lower() in ("y", "yes")
        return True
    except Exception as e:
        print(f"\r\n{label('Approval','warning')} {color_error(str(e))}\r\n", end="")
        # Fail-open to avoid blocking the user if the checker fails
        return True

# ----------------------------
# Terminal helpers
# ----------------------------

def get_winsize(fd: int) -> Tuple[int, int]:
    try:
        s = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        rows, cols, _xp, _yp = struct.unpack("HHHH", s)
        return rows or 24, cols or 80
    except Exception:
        return 24, 80

def set_winsize(fd: int, rows: int, cols: int) -> None:
    s = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, s)
    except Exception:
        pass

# ----------------------------
# Main PTY Terminal
# ----------------------------

class AITerminal:
    def __init__(self, dry_run: bool = False, quiet: bool = False):
        self.child_pid: Optional[int] = None
        self.master_fd: Optional[int] = None
        self.orig_tattr = None
        self.line_buffer = bytearray()
        self.last_output_lines: List[str] = []
        self.max_context_lines = 150

        # Error/repair context
        self.last_failed_command: Optional[str] = None
        self.last_error_text: Optional[str] = None
        self.last_repair_suggestion: Optional[str] = None

        # Per-command boundaries
        self._pending_cmd: Optional[str] = None
        self._pending_output_start: int = 0

        # Repair guard/state
        self._repair_in_progress: bool = False
        self._last_repaired_for_cmd: Optional[str] = None
        self._repair_attempts: Dict[str, int] = {}

        # Flags
        self.dry_run = dry_run
        self.quiet = quiet
        self.auto_repair = False
        self.interactive_repair = True

        # Slash-command typing mode
        self._typing_special_command = False

        # Output filtering buffer
        self._filter_buf = ""

        # Warm up LLM provider (best-effort)
        try:
            _ = get_llm()
        except Exception as e:
            print(f"Warning: LLM not available: {e}")

    # --- raw input helpers ---

    def _read_line_raw(self) -> str:
        """Simple raw line reader (local echo)."""
        buf = bytearray()
        while True:
            ch = os.read(sys.stdin.fileno(), 1)
            if ch in (b"\r", b"\n"):
                print("\r\n", end="")
                break
            if ch == b"\x03":  # Ctrl-C
                print("\r\n", end="")
                return ""
            if ch in (b"\x7f", b"\x08"):  # backspace
                if buf:
                    buf = buf[:-1]
                    os.write(sys.stdout.fileno(), b"\b \b")
                continue
            try:
                os.write(sys.stdout.fileno(), ch)
            except Exception:
                pass
            buf += ch
        return buf.decode("utf-8", "ignore").strip()

    def _prompt_fix_choice(self, failed_cmd: str, error_text: str, suggestion: str) -> str:
        print(f"\r\n{header('Command Failed','warning')}", end="\r\n")
        print(f"Failed: {code('$ ' + failed_cmd)}", end="\r\n")
        preview = (error_text or "").strip()
        if len(preview) > 200:
            preview = preview[:200] + "..."
        print(f"{color_error('Error:')} {preview}", end="\r\n")
        print(f"\r\n{success('Suggested fix:')} {code('$ ' + suggestion)}", end="\r\n")
        print(f"Choose: {dim('[r]un, [c]ancel, [e]dit, [p]lan (replan)')}: ", end="")
        sys.stdout.flush()
        ans = (self._read_line_raw() or "").lower()
        if ans.startswith("r"):
            return "run"
        if ans.startswith("e"):
            return "edit"
        if ans.startswith("p"):
            return "replan"
        return "cancel"

    # ---------- REPLAN SUPPORT (FIX FOR [p]) ----------

    def _prompt_replan_feedback(self) -> str:
        """Ask the user for optional guidance before calling llm_replan()."""
        print(f"\r\n{header('Plan')} Provide feedback or context (optional).", end="\r\n")
        print(f"{dim('Examples: what you were trying to do, constraints, preferred tools, etc.')}", end="\r\n")
        print(f"{dim('Press Enter to continue with no extra feedback.')} ", end="")
        sys.stdout.flush()
        fb = self._read_line_raw()
        return fb or ""

    def _format_plan_steps(self, steps) -> List[tuple]:
        """Normalize steps from llm_replan to [(command, explanation), ...]."""
        normalized: List[tuple] = []
        if isinstance(steps, list):
            for s in steps:
                if isinstance(s, dict):
                    cmd = (s.get("command") or s.get("cmd") or "").strip()
                    expl = (s.get("explanation") or s.get("why") or "").strip()
                    if cmd:
                        normalized.append((cmd, expl))
                elif isinstance(s, str):
                    normalized.append((s.strip(), ""))
        return normalized

    def _handle_replan(self, failed_cmd: str, feedback: str) -> None:
        """Call llm_replan() and let the user choose a step to run (gated)."""
        try:
            state: State = {
                "failed_command": failed_cmd or self.last_failed_command or "",
                "last_error": self.last_error_text or "",
                "user_feedback": feedback or "",
                "cwd": os.getcwd(),
                "dry_run": False,
                "quiet": True,
                "strict_json": True,
            }
            result = llm_replan(state) or {}
        except Exception as e:
            print(f"\r\n{label('Plan','warning')} {color_error(str(e))}\r\n", end="")
            return

        steps = self._format_plan_steps(result.get("steps") or result.get("plan") or [])
        if not steps:
            print(f"\r\n{label('Plan','muted')} No steps returned.\r\n", end="")
            return

        print(f"\r\n{header('Proposed Plan')}", end="\r\n")
        for i, (cmd, expl) in enumerate(steps, 1):
            line = f"  [{i}] {code(cmd)}"
            if expl:
                line += f"  {dim(expl)}"
            print(line, end="\r\n")

        print(f"\r\nChoose a step number to run, {dim('[e]dit <n>')} to edit, or press Enter to cancel: ", end="")
        sys.stdout.flush()
        choice = self._read_line_raw().strip().lower()

        if not choice:
            print(f"{dim('Cancelled.')}\r\n", end="")
            return

        # edit flow: "e 2" or "edit 2"
        if choice.startswith("e"):
            parts = choice.split()
            if len(parts) >= 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
            else:
                print(f"{label('Plan','warning')} Invalid edit selection.\r\n", end="")
                return
            if idx < 0 or idx >= len(steps):
                print(f"{label('Plan','warning')} Out-of-range selection.\r\n", end="")
                return
            cmd = steps[idx][0]
            # put the command on the bash line for user to edit
            try:
                os.write(self.master_fd, b"\x15")  # clear
            except Exception:
                pass
            for ch in cmd:
                os.write(self.master_fd, ch.encode())
            os.write(sys.stdout.fileno(), f"{code('$ ' + cmd)}\r\n".encode())
            return

        # numeric selection
        if not choice.isdigit():
            print(f"{label('Plan','warning')} Invalid selection.\r\n", end="")
            return
        idx = int(choice) - 1
        if idx < 0 or idx >= len(steps):
            print(f"{label('Plan','warning')} Out-of-range selection.\r\n", end="")
            return

        cmd = steps[idx][0]
        if not approval_gate(cmd, context=self.last_output_text()):
            print(f"\r\n{label('Plan','warning')} Command rejected.\r\n", end="")
            return

        self._pending_cmd = cmd
        self._pending_output_start = len(self.last_output_lines)
        self._send_command_immediately(cmd)

    # --- child / pty ---

    def _build_child_environment(self) -> None:
        """Install prompt & status hook silently BEFORE exec'ing bash."""
        status_marker = 'printf "\\n[[AI:STATUS:%d]]\\n" $?;'
        existing_pc = os.environ.get("PROMPT_COMMAND", "").strip()
        os.environ["PROMPT_COMMAND"] = (f"{status_marker} {existing_pc}".strip()
                                        if existing_pc else status_marker)

        os.environ["PS1"] = "\\[\x1b[1;36m\\]BashBard\\[\x1b[0m\\]$ "
        os.environ.setdefault("HISTCONTROL", "ignoredups")
        os.environ.setdefault("TERM", os.environ.get("TERM", "xterm-256color"))

    def spawn_shell(self) -> None:
        """Fork a PTY and exec bash in the child."""
        pid, mfd = pty.fork()
        if pid == 0:
            # ---- Child ----
            try:
                os.setsid()
            except Exception:
                pass
            self._build_child_environment()
            os.execvp(BASH[0], BASH)

        # ---- Parent ----
        self.child_pid = pid
        self.master_fd = mfd
        r, c = get_winsize(sys.stdin.fileno())
        set_winsize(self.master_fd, r, c)

    # --- TTY mode ---

    def enter_raw(self) -> None:
        """Put stdin into raw mode (if it's a TTY)."""
        try:
            if sys.stdin.isatty():
                self.orig_tattr = termios.tcgetattr(sys.stdin.fileno())
                tty.setraw(sys.stdin.fileno())
        except Exception:
            self.orig_tattr = None

    def restore_tattr(self) -> None:
        """Restore original terminal settings."""
        try:
            if self.orig_tattr and sys.stdin.isatty():
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.orig_tattr)
        except Exception:
            pass

    # --- signals ---

    def on_sigwinch(self, *_):
        if self.master_fd is None:
            return
        rows, cols = get_winsize(sys.stdin.fileno())
        set_winsize(self.master_fd, rows, cols)

    def forward_signal(self, sig: int) -> None:
        """Attempt to send signal to the foreground process group of the PTY."""
        if self.master_fd is None and self.child_pid is None:
            return
        sent = False
        # Prefer the PTY's foreground PGID
        try:
            if self.master_fd is not None:
                fg_pgid = os.tcgetpgrp(self.master_fd)
                if isinstance(fg_pgid, int) and fg_pgid > 0:
                    os.killpg(fg_pgid, sig)
                    sent = True
        except Exception:
            pass
        # Fallback: child's process group / pid
        if not sent and self.child_pid is not None:
            try:
                os.killpg(self.child_pid, sig)
                sent = True
            except Exception:
                try:
                    os.kill(self.child_pid, sig)
                    sent = True
                except Exception:
                    pass
        # Send control byte as last resort for interactive programs
        try:
            if self.master_fd is not None:
                ctrl_map = {signal.SIGINT: b"\x03", signal.SIGTSTP: b"\x1a", signal.SIGQUIT: b"\x1c"}
                ctrl = ctrl_map.get(sig)
                if ctrl:
                    os.write(self.master_fd, ctrl)
        except Exception:
            pass

    def install_handlers(self) -> None:
        try:
            signal.signal(signal.SIGWINCH, self.on_sigwinch)
            signal.signal(signal.SIGCHLD, lambda *_: None)  # allow select() to wake
        except Exception:
            pass

    # Kept for API compatibility (setup is already done in child env)
    def install_status_prompt(self) -> None:
        return

    def _send_command_immediately(self, command: str) -> None:
        if not isinstance(command, str) or self.master_fd is None:
            return
        try:
            os.write(self.master_fd, b"\x15")  # Ctrl-U clears the line
        except Exception:
            pass
        os.write(self.master_fd, (command + "\n").encode("utf-8"))

    # --- repair support ---

    def _should_attempt_repair(self, command: str) -> bool:
        return bool(command)

    def _try_auto_repair(self) -> None:
        if not (self.last_failed_command and self.last_error_text):
            return

        if not self.auto_repair:
            if self._should_attempt_repair(self.last_failed_command):
                if "command not found" in (self.last_error_text or "").lower():
                    print("\r\n" + label('HINT','muted') + " Use " + code("'/repair on'") + " to enable auto-repair for typos\r\n", end="")
            return

        if self._repair_in_progress:
            return
        if not self._should_attempt_repair(self.last_failed_command):
            return

        self._repair_in_progress = True
        try:
            self._repair_attempts[self.last_failed_command] = self._repair_attempts.get(self.last_failed_command, 0) + 1

            repaired = repair_command_if_needed(
                prev_cmd=self.last_failed_command,
                last_output_chunk=self.last_error_text,
                cwd=os.getcwd(),
                env=dict(os.environ),
            )
            if not (isinstance(repaired, str) and repaired.strip()):
                print(f"\r\n{label('AI','warning')} No automatic fix was generated.", end="\r\n")
                print(f"Choose: {dim('[p]lan (replan), [e]dit, [c]ancel')}: ", end="")
                sys.stdout.flush()
                choice = (self._read_line_raw() or "").lower()
                if choice.startswith("p"):
                    fb = self._prompt_replan_feedback()
                    self._handle_replan(self.last_failed_command, fb)
                elif choice.startswith("e"):
                    try:
                        os.write(self.master_fd, b"\x15")
                    except Exception:
                        pass
                    for ch in self.last_failed_command:
                        os.write(self.master_fd, ch.encode())
                    os.write(sys.stdout.fileno(), f"{code('$ ' + self.last_failed_command)}\r\n".encode())
                return

            self.last_repair_suggestion = repaired
            self._last_repaired_for_cmd = self.last_failed_command

            if self.interactive_repair:
                choice = self._prompt_fix_choice(self.last_failed_command, self.last_error_text or "", repaired)
                if choice == "cancel":
                    return
                if choice == "edit":
                    os.write(self.master_fd, b"\x15")
                    os.write(self.master_fd, repaired.encode("utf-8"))
                    os.write(sys.stdout.fileno(), f"{code('$ ' + repaired)}\r\n".encode())
                    return
                if choice == "replan":
                    fb = self._prompt_replan_feedback()
                    self._handle_replan(self.last_failed_command, fb)
                    return

            if not approval_gate(repaired, context=self.last_output_text()):
                print(f"\r\n{label('Repair','warning')} Repaired command rejected\r\n", end="")
                return

            self._pending_cmd = repaired
            self._pending_output_start = len(self.last_output_lines)
            self._send_command_immediately(repaired)
        finally:
            self._repair_in_progress = False

    # --- context ---

    def append_output_context(self, data: bytes) -> None:
        text = data.decode("utf-8", "replace")
        for raw_line in text.splitlines():
            line = raw_line.strip()
            self.last_output_lines.append(raw_line)
            if self._pending_cmd is not None and "command not found" in line.lower():
                start = max(0, self._pending_output_start)
                self.last_failed_command = self._pending_cmd
                self.last_error_text = "\n".join(self.last_output_lines[start:])
                self._try_auto_repair()
                self._pending_cmd = None
                self._pending_output_start = len(self.last_output_lines)
                continue
            m = STATUS_LINE_RE.match(line)
            if m:
                try:
                    exit_code = int(m.group(1))
                except Exception:
                    exit_code = 0
                if self._pending_cmd is not None:
                    if exit_code != 0:
                        start = max(0, self._pending_output_start)
                        err_lines = self.last_output_lines[start:-1]
                        self.last_failed_command = self._pending_cmd
                        self.last_error_text = "\n".join(err_lines)
                        self._try_auto_repair()
                    else:
                        self._repair_attempts.pop(self._pending_cmd, None)
                    self._pending_cmd = None
                    self._pending_output_start = len(self.last_output_lines)
        if len(self.last_output_lines) > self.max_context_lines:
            self.last_output_lines = self.last_output_lines[-self.max_context_lines:]

    def last_output_text(self) -> str:
        return "\n".join(self.last_output_lines)

    # --- core: intercept Enter, slash-commands, approval gate ---

    def gate_and_send(self, user_line_utf8: str) -> None:
        original_line = user_line_utf8.rstrip("\r\n")
        line = original_line

        if not line.strip():
            if self.master_fd is not None:
                os.write(self.master_fd, b"\n")
            return

        stripped = line.lstrip()
        if stripped.startswith("/"):
            # Clear bash readline just in case some chars leaked
            try:
                if self.master_fd is not None:
                    os.write(self.master_fd, b"\x15")  # Ctrl-U
            except Exception:
                pass

            tokens = stripped.split()
            cmd = tokens[0]
            args = tokens[1:]

            if cmd in ("/q", "/quit", "/exit"):
                if self.master_fd is not None:
                    os.write(self.master_fd, b"exit\n")
                return

            elif cmd == "/help":
                help_text = (
                    f"\r\n{header('BashBard Commands')}\r\n"
                    f"  {code('/e <request>')}   Convert natural language to command\r\n"
                    f"  {code('/repair on')}     Enable auto-repair (interactive approval)\r\n"
                    f"  {code('/repair auto')}   Enable auto-repair and auto-run fixes\r\n"
                    f"  {code('/repair off')}    Disable auto-repair (default)\r\n"
                    f"  {code('/dry on')}        Enable dry-run mode\r\n"
                    f"  {code('/dry off')}       Disable dry-run mode\r\n"
                    f"  {code('/dry status')}    Show dry-run status\r\n"
                    f"  {code('/alias basic')}   Enable ll/la/l aliases\r\n"
                    f"  {code('/help')}          Show this help\r\n"
                    f"  {code('/quit')}          Exit terminal\r\n\r\n"
                )
                os.write(sys.stdout.fileno(), help_text.encode())
                if self.master_fd is not None:
                    os.write(self.master_fd, b"\n")
                return

            elif cmd == "/repair":
                sub = (args[0].lower() if args else "on")
                if sub in ("on", "interactive"):
                    self.auto_repair = True
                    self.interactive_repair = True
                    os.write(sys.stdout.fileno(), f"\r\n{label('REPAIR','success')} Auto-repair enabled with interactive approval\r\n".encode())
                elif sub == "auto":
                    self.auto_repair = True
                    self.interactive_repair = False
                    os.write(sys.stdout.fileno(), f"\r\n{label('REPAIR','success')} Auto-repair enabled: auto-run fixes\r\n".encode())
                elif sub == "off":
                    self.auto_repair = False
                    os.write(sys.stdout.fileno(), f"\r\n{label('REPAIR','muted')} Auto-repair disabled\r\n".encode())
                else:
                    os.write(sys.stdout.fileno(), f"\r\n{label('Usage','muted')} /repair [on|interactive|auto|off]\r\n".encode())
                if self.master_fd is not None:
                    os.write(self.master_fd, b"\n")
                return

            # /dry works like /repair on: default is "on"
            elif cmd == "/dry":
                sub = (args[0].lower() if args else "on")
                if sub in ("on", "enable"):
                    self.dry_run = True
                    os.write(sys.stdout.fileno(), f"\r\n{label('DRY-RUN','success')} Dry-run enabled — commands will NOT be executed\r\n".encode())
                elif sub in ("off", "disable"):
                    self.dry_run = False
                    os.write(sys.stdout.fileno(), f"\r\n{label('DRY-RUN','muted')} Dry-run disabled — commands will execute normally\r\n".encode())
                elif sub == "status":
                    status = "ENABLED" if self.dry_run else "DISABLED"
                    tone = 'success' if self.dry_run else 'muted'
                    os.write(sys.stdout.fileno(), f"\r\n{label('DRY-RUN', tone)} Status: {status}\r\n".encode())
                else:
                    os.write(sys.stdout.fileno(), f"\r\n{label('Usage','muted')} /dry [on|off|status]\r\n".encode())
                if self.master_fd is not None:
                    os.write(self.master_fd, b"\n")
                return

            elif cmd == "/alias":
                sub = (args[0].lower() if args else "")
                if sub == "basic":
                    self._send_command_immediately("alias ll='ls -alF'; alias la='ls -A'; alias l='ls -CF'")
                else:
                    os.write(sys.stdout.fileno(), f"\r\n{label('Usage','muted')} /alias basic\r\n".encode())
                    if self.master_fd is not None:
                        os.write(self.master_fd, b"\n")
                return

            elif cmd.startswith("/e"):
                remainder = stripped[len("/e"):].strip()
                if remainder:
                    os.write(sys.stdout.fileno(), b"\r\n")
                    transformed = english_to_command_if_needed(
                        remainder, cwd=os.getcwd(), env=dict(os.environ),
                    ) or ""
                    transformed = transformed.strip()
                    if transformed:
                        os.write(sys.stdout.fileno(), f"{code('$ ' + transformed)}\r\n".encode())
                        line = transformed  # fall through to approval/execution below
                    else:
                        print(f"{label('AI','muted')} No command generated.", end="\r\n")
                        print(f"Choose: {dim('[p]lan (replan), [e]dit, [c]ancel')}: ", end="")
                        sys.stdout.flush()
                        choice = (self._read_line_raw() or "").lower()
                        if choice.startswith("p"):
                            fb = self._prompt_replan_feedback()
                            self._handle_replan("", fb)
                        elif choice.startswith("e"):
                            if self.master_fd is not None:
                                os.write(self.master_fd, b"\x15")
                            os.write(sys.stdout.fileno(), b"\r\n")
                        else:
                            if self.master_fd is not None:
                                os.write(self.master_fd, b"\n")
                        return
                else:
                    os.write(sys.stdout.fileno(), f"\r\n{label('Usage','muted')} /e <natural language request>\r\n".encode())
                    if self.master_fd is not None:
                        os.write(self.master_fd, b"\n")
                    return
            else:
                # Unknown /-prefixed -> let bash handle it explicitly
                if self.master_fd is not None:
                    os.write(self.master_fd, (line + "\n").encode("utf-8"))
                return

        # 🔒 safety approval for ALL commands (user-typed too)
        if not approval_gate(line, context=self.last_output_text()):
            os.write(sys.stdout.fileno(), f"\r\n{label('Approval','warning')} Command rejected\r\n".encode())
            if self.master_fd is not None:
                os.write(self.master_fd, b"\n")
            return

        if self.dry_run:
            os.write(sys.stdout.fileno(), f"\r\n{label('DRY-RUN','muted')} Would execute: {code('$ ' + line)}\r\n".encode())
            if self.master_fd is not None:
                os.write(self.master_fd, b"\n")
            return

        self._pending_cmd = line
        self._pending_output_start = len(self.last_output_lines)

        if self.master_fd is not None:
            if line == original_line:
                os.write(self.master_fd, b"\n")
            else:
                self.install_status_prompt()  # no-op
                os.write(self.master_fd, (line + "\n").encode("utf-8"))

    # --- main loop (I/O + filtering) ---

    def _filter_and_write(self, chunk: bytes) -> None:
        """
        Clean view:
        - Hide status markers and absl/gRPC noise.
        - Print prompt/partial (no newline) immediately if it doesn't contain a marker.
        """
        s = chunk.decode("utf-8", "replace")
        self._filter_buf += s

        if "\n" not in self._filter_buf:
            if STATUS_INLINE not in self._filter_buf and not any(x in self._filter_buf for x in SUPPRESS_SUBSTRINGS):
                os.write(sys.stdout.fileno(), self._filter_buf.encode("utf-8"))
                self._filter_buf = ""
            return

        parts = self._filter_buf.splitlines(keepends=True)
        tail = "" if parts[-1].endswith(("\n", "\r")) else parts.pop()

        out_pieces = []
        for p in parts:
            if STATUS_INLINE in p:
                continue
            if any(x in p for x in SUPPRESS_SUBSTRINGS):
                continue
            out_pieces.append(p)

        if out_pieces:
            os.write(sys.stdout.fileno(), "".join(out_pieces).encode("utf-8"))

        if tail and (STATUS_INLINE not in tail) and (not any(x in tail for x in SUPPRESS_SUBSTRINGS)):
            os.write(sys.stdout.fileno(), tail.encode("utf-8"))
            self._filter_buf = ""
        else:
            self._filter_buf = tail

    def run(self) -> None:
        _print_banner()
        print(dim("Type '/help' for available commands"))
        print(dim("Tip: Enable '/repair on' to auto-fix typos like 'lsf' → 'ls'"))
        print(dim("Tip: Use '/alias basic' to enable ll/la/l"))
        print(dim("Tip: /e <request>   Convert natural language to command\n"))


        self.spawn_shell()
        self.install_handlers()
        self.install_status_prompt()  # no-op
        self.enter_raw()

        try:
            while True:
                r, _, _ = select.select([self.master_fd, sys.stdin], [], [])
                # PTY → user
                if self.master_fd in r:
                    try:
                        data = os.read(self.master_fd, 65536)
                        if not data:
                            break
                        self.append_output_context(data)
                        self._filter_and_write(data)
                    except OSError:
                        break

                # user → PTY
                if sys.stdin in r:
                    ch = os.read(sys.stdin.fileno(), 1)
                    if not ch:
                        break

                    # Signals
                    if ch == b"\x03":  # Ctrl-C
                        self.forward_signal(signal.SIGINT)
                        self.line_buffer.clear()
                        self._typing_special_command = False
                        continue
                    if ch == b"\x1a":  # Ctrl-Z
                        self.forward_signal(signal.SIGTSTP)
                        continue
                    if ch == b"\x1c":  # Ctrl-\
                        self.forward_signal(signal.SIGQUIT)
                        continue

                    # Editing
                    if ch in (b"\x7f", b"\x08"):  # backspace
                        if self.line_buffer:
                            self.line_buffer = self.line_buffer[:-1]
                            if not self.line_buffer:
                                self._typing_special_command = False
                        # Echo locally if in slash-mode; otherwise pass to PTY
                        if self._typing_special_command:
                            os.write(sys.stdout.fileno(), b"\b \b")
                        else:
                            if self.master_fd is not None:
                                os.write(self.master_fd, ch)
                        continue

                    # Enter → intercept whole line
                    if ch in (b"\r", b"\n"):
                        try:
                            line = self.line_buffer.decode("utf-8", "replace")
                        finally:
                            self.line_buffer.clear()
                            self._typing_special_command = False
                        self.gate_and_send(line)
                        continue

                    # Regular printable char
                    was_empty = (len(self.line_buffer) == 0)
                    self.line_buffer += ch

                    # If the first char typed is '/', switch to special-mode,
                    # CLEAR bash's current line (Ctrl-U), and echo locally.
                    if was_empty and ch == b"/":
                        self._typing_special_command = True
                        try:
                            if self.master_fd is not None:
                                os.write(self.master_fd, b"\x15")  # Ctrl-U
                        except Exception:
                            pass
                        os.write(sys.stdout.fileno(), ch)
                        continue

                    if self._typing_special_command:
                        # Do not send to PTY when typing slash-commands; echo locally
                        os.write(sys.stdout.fileno(), ch)
                    else:
                        # Normal bash editing (readline, history, completion)
                        if self.master_fd is not None:
                            os.write(self.master_fd, ch)

        finally:
            if self._filter_buf:
                os.write(sys.stdout.fileno(), self._filter_buf.encode("utf-8"))
                self._filter_buf = ""
            self.restore_tattr()
            try:
                if self.child_pid:
                    os.kill(self.child_pid, signal.SIGHUP)
            except Exception:
                pass
            if self.child_pid:
                try:
                    os.waitpid(self.child_pid, 0)
                except Exception:
                    pass

def main():
    """Entry point for standalone terminal usage."""
    import argparse
    # Optional dotenv support (no hard dependency)
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        def load_dotenv(*_args, **_kwargs):  # no-op fallback
            return

    load_dotenv()

    parser = argparse.ArgumentParser(description="BashBard AI Terminal")
    parser.add_argument("--dry-run", action="store_true", help="Enable dry-run mode")
    parser.add_argument("--quiet", action="store_true", help="Reduce output verbosity")
    parser.add_argument("--auto-repair", action="store_true", help="Enable auto-repair on start")
    args = parser.parse_args()

    terminal = AITerminal(dry_run=args.dry_run, quiet=args.quiet)
    if args.auto_repair:
        terminal.auto_repair = True

    terminal.run()

if __name__ == "__main__":
    main()
