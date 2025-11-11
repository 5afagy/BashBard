[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![LLM: Google Gemini](https://img.shields.io/badge/LLM-Google_Gemini-brightgreen.svg?logo=google)](https://ai.google.dev)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg?logo=python&logoColor=white)](https://python.org)
[![Visitors](https://visitor-badge.laobi.icu/badge?page_id=5afagy.BashBard&left_color=grey&right_color=brightgreen)](https://github.com/5afagy/BashBard)
[![Wiki](https://img.shields.io/badge/Wiki-BashBard-blue)](https://github.com/5afagy/BashBard/wiki)
[![Languages](https://img.shields.io/badge/Input-Multilingual_(Arabic_+_More)-purple.svg)](#-multilingual-input-support)
<!--[![Downloads](https://img.shields.io/github/downloads/5afagy/BashBard/total.svg?label=downloads)](https://github.com/5afagy/BashBard/releases)-->


<div align="center">

# <span style="font-size: 1.8em;">🧠</span> **BashBard**  

[Demo_v1.webm](https://github.com/user-attachments/assets/1494963f-7390-4119-a7e9-b44e35608c05)

### *AI-Powered Shell Intelligence & Safety Layer*

> **Think in any language. Execute in Bash. Never regret a command.**

</div>

### ⚡Overview

**BashBard** is an AI-augmented terminal assistant that embeds **Google Gemini** intelligence directly into your Linux shell via a true PTY environment.

It transforms ambiguous intent into **optimized, vetted bash commands**, repairs failed executions in real time, and enforces **proactive safety**, all without leaving your terminal.

Designed for:
- **Developers** writing scripts under pressure  
- **Sysadmins** managing critical infrastructure  
- **Security engineers** auditing and responding at speed  
- **DevOps teams** automating with precision  
- **Power users** who demand fluency and control

BashBard delivers **smarter automation**, **zero-trust execution**, **context-aware correction** and seamlessly integrated into your workflow.

---

## 💡 Why BashBard Exists

The terminal is the most powerful interface in computing and the most dangerous.

A single typo can wipe data. A forgotten flag can leak secrets. A copied one-liner can compromise a system.

**BashBard eliminates these risks at the source:**

| Pain Point | BashBard Solution |
|-----------|-------------------|
| **Syntax amnesia** | `/e find all config files modified today` → `find /etc -type f -mtime -1` |
| **Error cascade** | Captures `stderr`, re-plans, and corrects — automatically or on demand |
| **Destructive mistakes** | AI safety layer blocks `rm -rf /`, `> /etc/passwd`, `curl | bash` — requires explicit approval |
| **Repetitive drudgery** | Learns patterns, suggests aliases, enables one-shot automation |

> **“Your terminal, now with judgment.”**

## Multilingual Input Support

BashBard understands **natural language in any language** — including **Arabic**, **English**, **French**, **Spanish**, and more.


## ⚙️ Quick Start (30 Seconds)

BashBard ships with an automated installer that sets up everything for you.

```bash
curl -sSL https://github.com/5afagy/BashBard/raw/refs/heads/main/install.sh | bash && BashBard
```

> **One command. Zero hassle.**  
> Installs, configures, and launches BashBard even on fresh systems.

### What the installer does:
- Creates a **dedicated virtual environment**
- Installs dependencies from `requirements.txt`
- Prompts for your **Google Gemini API key** (on first run)
- Auto-generates `.env` at `~/.local/share/bashbard/.env`
- Installs launcher to `~/.local/bin/BashBard`

**Example first-run output:**

```bash
Configuring BashBard environment...
Installation complete.
Enter your Google Gemini API key: ??
Saved API key to /root/.local/share/bashbard/.env
```

## Usage Examples
<img width="1093" height="721" alt="image" src="https://github.com/user-attachments/assets/6123b1eb-0cf3-4308-92ca-e6735bd578d8" />


BashBard understands your intent, it’s not just syntax-aware, it’s *context-aware.*

<!--### Uninstall

```bash
rm -rf ~/.local/share/bashbard ~/.local/bin/BashBard
``` -->
## Core Features

| Feature | How to Use | Live Example |
|--------|------------|--------------|
| **Natural Language → Command** | `/e <request>` | <pre>/e list all .log files older than 7 days<br>→ find /var/log -name "*.log" -mtime +7</pre> |
| **Auto-Repair (Interactive)** | `/repair on` | <pre>gti status<br>AI: Did you mean `git status`?<br>[r]un / [e]dit / [c]ancel</pre> |
| **Auto-Repair (Silent)** | `/repair auto` | <pre>lsf<br>→ ls -F (runs automatically)</pre> |
| **Disable Auto-Repair** | `/repair off` | No AI fixes, default mode |
| **Dry-Run Mode** | `/dry on` | <pre>/e delete temp files<br>Would execute: rm -rf /tmp/*</pre> |
| **Dry-Run Status** | `/dry status` | `DRY-RUN: ENABLED` |
| **Quick Aliases** | `/alias basic` | <pre>ll → ls -alF<br>la → ls -A<br>l → ls -CF</pre> |
| **Replan on Failure** | Choose `[p]lan` after error | <pre>docker run -p 80:80 app<br>AI Plan:<br>[1] docker pull app<br>[2] docker run -p 8080:80 app</pre> |
| **Universal Safety Gate** | **All commands** (even typed) | <pre>rm -rf /home<br>DANGEROUS: Targets root-owned path<br>Why risky: irreversible data loss<br>Proceed? [y/N]</pre> |
| **Help & Exit** | `/help` or `/quit` | Shows full command reference |

> **Pro Workflow:**  
> 1. Start with `/repair on` + `/dry on` → explore safely  
> 2. Trust the flow? → `/repair auto` for speed  
> 3. Need full control? → `/dry off` + manual approval


### ⚙️ **Advanced Features**

* Context-aware **auto-repair** with re-planning (`/repair auto`)
* Intelligent **output filtering** for clean AI feedback
* Quick **alias injection** (`/alias basic`)
* Customizable **dry-run**, **quiet**, and **auto-repair** modes
* Built-in **danger heuristics** to prevent destructive actions

> 💡 **Pro Tip:** Run `/repair on` to let BashBard automatically fix simple typos like `gti` → `git`.

## Technology Overview

BashBard is engineered with a modular architecture built on modern AI and system libraries.

**Stack:**

* Python 3.12+
* Google Gemini (default) via **LangChain / LangGraph**
* PTY shell system (using `pty`, `termios`, and `fcntl`)

**Core engine:** `terminal.py` runs the real-time AI terminal.
**CLI wrapper:** `cli.py` decides mode (interactive, legacy, one-shot).
**LLM logic:** `llm.py`, `nodes.py`, and `safety.py` handle intelligence and protection.

## Future Roadmap

| Feature | Description |
|--------|-------------|
| **Multi-model support** | Gemini + OpenAI + local models (cost/latency balance) |
| **Session memory** | Retain context across commands, privacy-first |
| **Offline mode** | Run fully on-prem or air-gapped |
| **Plugin API** | Extend with Docker, K8s, AWS, pentest modules |
| **Safe sandboxes** | Test risky commands in isolation |
| **Pentester mode** | Evidence capture, checklists, exploit guidance |
| **History intelligence** | Auto-refactor repeated tasks |
| **Explainability** | Show prompt + reasoning for every suggestion |
| **Team sync (opt-in)** | Share policies, not data |

*All telemetry and cloud features are **opt-in only**.*

## 🤝 How You Can Help (Open to Contributors)

BashBard is a community project, contributions are welcome. Ways to contribute:

- ⭐ Star & fork the repo  
- 🐛 Open issues for bugs or improvement ideas  
- ✍️ Submit pull requests (features, docs, tests)  
- 💬 Join Discussions to propose roadmap items or plugins  
- 🔒 Help with security reviews and safe execution patterns

**Contribution guide:** Fork → branch → PR → review. Keep changes focused and well-documented.

## 🌟 Contributors

<a href="https://github.com/5afagy/BashBard/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=5afagy/BashBard" />
</a>

## 👨‍💻 Authors & License

**Authors:** [Khafagy](https://github.com/5afagy) & [Elnaggar](https://github.com/hungryDev399)  
**Affiliated with:** [Cyber Force LLC](https://www.cyber-force.org/)  
**License:** [Apache 2.0](https://opensource.org/licenses/Apache-2.0)

> Built with ❤️ in **Egypt** — open to collaborators worldwide.

---

<div align="center">

## **BashBard: Intelligence Meets the Command Line.**

[View on GitHub](https://github.com/5afagy/BashBard) • [Wiki](https://github.com/5afagy/BashBard/wiki) • [Discussions](https://github.com/5afagy/BashBard/discussions)

**Star. Fork. Build. Secure.**

```bash
curl -sSL https://github.com/5afagy/BashBard/raw/main/install.sh -o install.sh && bash install.sh && BashBard
```

<p align="center">
  <sub>Updated: <b>November 11, 2025 04:15 PM EET</b> • Cairo, Egypt</sub>
</p>

</div>
