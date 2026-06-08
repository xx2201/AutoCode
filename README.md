# AutoCode

> Formerly **AutoCode** (and earlier **NanoCoder**) — renamed to avoid confusion and align the CLI with the product name.


[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/AutoCode/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/AutoCode/actions)

[中文](README_CN.md) | [English](README.md) | [Claude Code Architecture Deep Dive (7 articles)](article/)

**512,000 lines of TypeScript → ~1,400 lines of Python.**

I spent two days reverse-engineering the leaked Claude Code source — all half a million lines. Then I stripped it down to the load-bearing walls and rebuilt them in Python. The result: **every key architectural pattern from Claude Code, in a codebase you can read in one sitting.**

AutoCode is not another AI coding tool. It's a **blueprint** — the [nanoGPT](https://github.com/karpathy/nanoGPT) of coding agents. Read it, fork it, build your own.

---

```
$ autocode -m kimi-k2.5

You > read main.py and fix the broken import

  > read_file(file_path='main.py')
  > edit_file(file_path='main.py', ...)

--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-from utils import halper
+from utils import helper

Fixed: halper → helper.
```

## What You Get

Claude Code's 512K lines distilled into ~1,400 lines across 7 patterns that actually matter:

| Pattern | Claude Code | AutoCode |
|---|---|---|
| Search-and-replace editing (unique match + diff) | FileEditTool | `tools/edit.py` — 70 lines |
| Parallel tool execution | StreamingToolExecutor (530 lines) | `agent.py` — ThreadPool |
| 3-layer context compression | HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE | `context.py` — 145 lines |
| Sub-agent with isolated context | AgentTool (1,397 lines) | `tools/agent.py` — 50 lines |
| Dangerous command blocking | BashTool (1,143 lines) | `tools/bash.py` — 95 lines |
| Session persistence | QueryEngine (1,295 lines) | `session.py` — 65 lines |
| Dynamic system prompt | prompts.ts (914 lines) | `prompt.py` — 35 lines |

Every pattern is a real, runnable implementation — not a diagram or a blog post.

## Install

```bash
pip install -e .
```

Pick your model — any OpenAI-compatible API works. You can `export` env vars or drop a `.env` file in your project root:

```bash
# Kimi K2.5
export OPENAI_API_KEY=your-key OPENAI_BASE_URL=https://api.moonshot.ai/v1
autocode -m kimi-k2.5

# Claude Opus 4.6 (via OpenRouter)
export OPENAI_API_KEY=your-key OPENAI_BASE_URL=https://openrouter.ai/api/v1
autocode -m anthropic/claude-opus-4-6

# OpenAI GPT-5
export OPENAI_API_KEY=sk-...
autocode -m gpt-5

# DeepSeek V3
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com
autocode -m deepseek-chat

# Qwen 3.5
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
autocode -m qwen-max

# Ollama (local)
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1
autocode -m qwen3:32b

# One-shot mode
autocode -p "add error handling to parse_config()"
```

### Non-OpenAI providers (Bedrock, Vertex, Cohere, …)

For providers without an OpenAI-compatible endpoint, install the optional LiteLLM extra:

```bash
pip install -e '.[litellm]'

export AUTOCODE_PROVIDER=litellm
export AUTOCODE_MODEL=anthropic/claude-3-haiku   # any LiteLLM model string
export ANTHROPIC_API_KEY=sk-ant-...
autocode
```

LiteLLM routes through to 100+ providers (Bedrock, Vertex AI, Cohere, Groq, Replicate, Anyscale, etc.) using one model-string convention. The default `openai` backend is unchanged.

### Telegram Remote Control (Experimental)

If you want to control AutoCode from your phone, install the optional Telegram extra:

```bash
pip install -e '.[telegram]'

export AUTOCODE_MODEL=gpt-5
export AUTOCODE_API_KEY=sk-...
export AUTOCODE_TELEGRAM_BOT_TOKEN=123456:telegram-token
export AUTOCODE_TELEGRAM_ALLOWED_CHATS=123456789
autocode-telegram
```

The Telegram adapter is intentionally minimal and reuses the existing task runtime:

- send any text to start or continue a coding task
- `/task` shows the current task state
- `/trace` shows the latest trace summary
- `/approve` / `/reject` resolve pending approvals
- `/approve_all` resolves the current approval and auto-approves later normal confirms for the same task
- `/resume <task_id>` restores a saved checkpoint into the chat

This follows the same channel-adapter idea used by projects like `claw0`, but keeps AutoCode single-channel and lightweight instead of growing into a full gateway.

### Feishu Remote Control (Experimental)

If you want the same remote control workflow inside Feishu, install the optional Feishu extra:

```bash
pip install -e '.[feishu]'

export AUTOCODE_MODEL=gpt-5
export AUTOCODE_API_KEY=sk-...
export AUTOCODE_FEISHU_APP_ID=cli_xxx
export AUTOCODE_FEISHU_APP_SECRET=xxx
# optional allowlists
export AUTOCODE_FEISHU_ALLOWED_OPEN_IDS=ou_xxx
export AUTOCODE_FEISHU_ALLOWED_CHAT_IDS=oc_xxx
autocode-feishu
```

The Feishu adapter uses the official app-bot + long-connection event model:

- send any text to start or continue a coding task
- `/task`, `/tasks`, `/trace`, `/resume`, `/reset` work like Telegram
- `/approve`, `/approve_all`, `/reject` also work as text commands
- pending approvals are sent as interactive cards with **Approve / Approve All / Reject** buttons
- the bot reuses the existing task runtime and approval state instead of adding a second workflow engine

This keeps the implementation compact while still supporting full two-way control from Feishu without exposing a public webhook endpoint.

## Architecture

The whole thing fits in your head:

```
autocode/
├── cli.py            REPL + commands               218 lines
├── agent.py          Agent loop + parallel tools    122 lines
├── llm.py            Streaming client + retry       156 lines
├── context.py        3-layer compression            196 lines
├── session.py        Save/resume                     68 lines
├── prompt.py         System prompt                   33 lines
├── config.py         Env config                      55 lines
└── tools/
    ├── bash.py       Shell + safety + cd tracking   115 lines
    ├── edit.py       Search-replace + diff            85 lines
    ├── read.py       File reading                     53 lines
    ├── write.py      File writing                     36 lines
    ├── glob_tool.py  File search                      47 lines
    ├── grep.py       Content search                   78 lines
    └── agent.py      Sub-agent spawning               58 lines
```

## Use as a Library

```python
from autocode import Agent, LLM

llm = LLM(model="kimi-k2.5", api_key="your-key", base_url="https://api.moonshot.ai/v1")
agent = Agent(llm=llm)
response = agent.chat("find all TODO comments in this project and list them")
```

## Add Your Own Tools (~20 lines)

```python
from autocode.tools.base import Tool

class HttpTool(Tool):
    name = "http"
    description = "Fetch a URL."
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def execute(self, url: str) -> str:
        import urllib.request
        return urllib.request.urlopen(url).read().decode()[:5000]
```

## Commands

```
/model           Show current model
/model <name>    Switch model mid-conversation
/compact         Compress context (like Claude Code's /compact)
/tokens          Token usage + cost estimate
/diff            Show files modified this session
/save            Save session to disk
/sessions        List saved sessions
/reset           Clear history
quit             Exit
```

Saved session IDs are sanitized before they become filenames, so resume data stays inside `~/.autocode/sessions`.

Telegram commands:

```
/start           Show Telegram help
/task            Show current task state
/tasks           List recent checkpoints
/trace           Show trace for the current task
/approve         Approve the pending tool call
/approve_all     Approve this tool call and auto-approve later normal confirms
/reject          Reject the pending tool call
/resume <id>     Resume a saved task checkpoint into this chat
/reset           Clear the in-memory chat session
```

Feishu commands:

```
/start           Show Feishu help
/task            Show current task state
/tasks           List recent checkpoints
/trace           Show the current task trace
/approve         Approve the pending tool call
/approve_all     Approve this tool call and auto-approve later normal confirms
/reject          Reject the pending tool call
/resume <id>     Resume a saved task checkpoint into this chat
/reset           Clear the in-memory chat session
```

## How It Compares

|  | Claude Code | Claw-Code | Aider | AutoCode |
|---|---|---|---|---|
| Code | 512K lines (closed) | 100K+ lines | 50K+ lines | **~1,400 lines** |
| Models | Anthropic only | Multi | Multi | **Any OpenAI-compatible** |
| Readable? | No | Hard | Medium | **One afternoon** |
| Purpose | Use it | Use it | Use it | **Understand it, build yours** |

## The Deep Dive

I wrote [7 articles](article/) breaking down Claude Code's architecture — the agent loop, tool system, context compression, streaming executor, multi-agent, and 44 hidden feature flags. If you want to understand *why* AutoCode is designed this way, start there.

## FAQ

**Does AutoCode support Skills / Subagents / MCP?**

No, and that's intentional. AutoCode is the minimal runnable core — agent loop, tools, streaming, compaction. Skills, Subagents, MCP, hooks, and plugins are upper-layer features that Claude Code layers on top; if AutoCode had them too it would stop being a teaching artifact. The architecture articles above cover how those systems work in Claude Code, so you can add them yourself if you need to.

If you want Skills specifically, the recipe is small: scan `~/.claude/skills/*.md` at startup, list their titles in the system prompt, and let the agent ask for a skill by name before you inline that file's body into the conversation.

## Related Projects

- **[CodeJoust](https://github.com/he-yufeng/CodeJoust)** — a CLI arena that races Claude Code, aider, Codex, and Gemini (Cursor + OpenHands next) on the same bug in isolated git worktrees, scores by tests+cost+diff+time, hands you the winning patch. If you ever wondered *which* AI coding CLI is actually better for your task, CodeJoust answers it empirically.
- **[AnyCoder](https://github.com/he-yufeng/AnyCoder)** — a practical terminal AI coding agent built on the same architecture as AutoCode but with litellm, session persistence, and 100+ model support. Use this one if you want a tool; use AutoCode if you want to read source.
- **[LiteBench](https://github.com/he-yufeng/LiteBench)** — one-command LLM / agent benchmark. Ships 7 built-in tasks (HumanEval/GSM8K/MMLU/...) and YAML-defined custom tasks, with a single-file HTML dashboard.
- **[RepoWiki](https://github.com/he-yufeng/RepoWiki)** — open-source DeepWiki alternative. `pip install repowiki`, one command to turn any local or GitHub repo into a wiki with dependency graph, architecture diagram, and LLM-generated module pages.

## License

MIT. Fork it, learn from it, ship something better. A mention of this project is appreciated.

---

Built by **[Yufeng He](https://github.com/he-yufeng)** · Agentic AI Researcher @ Moonshot AI (Kimi)

[Claude Code Source Analysis — 170K+ reads, 6000 bookmarks on Zhihu](https://zhuanlan.zhihu.com/p/1898797658343862272)

