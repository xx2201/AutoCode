# AutoCode

> Formerly **AutoCode** (and earlier **NanoCoder**) — renamed to avoid confusion and align the CLI with the product name.

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/AutoCode/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/AutoCode/actions)

[中文](README_CN.md) | [English](README.md)

**Claude Code patterns, rebuilt as a lightweight local coding agent runtime.**

I spent two days reverse-engineering the leaked Claude Code source — all half a million lines. Then I stripped it down to the load-bearing walls and rebuilt the core ideas in Python. The result is still compact enough to study directly, but it has now grown beyond a toy loop into a usable local runtime with approvals, checkpoints, trace logs, remote adapters, and a local eval harness.

## What You Get

AutoCode now gives you two things at once:

- a readable coding-agent core you can fork and extend
- a practical local runtime with safety, recovery, observability, remote control, and evaluation

The core patterns it keeps from Claude Code are still the same:

| Pattern | Claude Code | AutoCode |
|---|---|---|
| Search-and-replace editing (unique match + diff) | FileEditTool | `autocode/tools/edit.py` |
| Parallel tool execution | StreamingToolExecutor | `autocode/runtime/engine.py` |
| 3-layer context compression | HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE | `autocode/context/manager.py` |
| Sub-agent with isolated context | AgentTool | `autocode/tools/agent.py` |
| Dangerous command blocking | BashTool | `autocode/runtime/policy.py` + `autocode/tools/bash.py` |
| Session resume + task state | QueryEngine-style runtime state | `autocode/state/` |
| Dynamic system prompt | prompts.ts | `autocode/context/prompt.py` |

Every pattern is a real, runnable implementation — not a diagram or a blog post.

What changed versus the original "minimal core" pitch:

- the repo is no longer just a tiny single-package demo
- `autocode/` is now organized into `agent / context / infra / runtime / state / tools / remote`
- besides the core agent loop, the repo now includes Telegram and Feishu adapters plus an independent local eval system under `eval/`

## Install

```bash
pip install -e .
```

Pick your model. By default AutoCode reads `AUTOCODE_*` environment variables and talks to any OpenAI-compatible endpoint:

```bash
# Kimi K2.5
export AUTOCODE_API_KEY=your-key AUTOCODE_BASE_URL=https://api.moonshot.ai/v1
export AUTOCODE_MODEL=kimi-k2.5
autocode

# OpenAI GPT-5
export AUTOCODE_API_KEY=sk-...
export AUTOCODE_MODEL=gpt-5
autocode

# Ollama (local)
export AUTOCODE_API_KEY=ollama AUTOCODE_BASE_URL=http://localhost:11434/v1
export AUTOCODE_MODEL=qwen3:32b
autocode

# One-shot mode
autocode -p "add error handling to parse_config()"
```

For non-OpenAI providers, install the LiteLLM extra:

```bash
pip install -e '.[litellm]'

export AUTOCODE_PROVIDER=litellm
export AUTOCODE_MODEL=anthropic/claude-3-haiku
export ANTHROPIC_API_KEY=sk-ant-...
autocode
```

## Architecture

```text
autocode/
├── cli.py
├── llm.py
├── config.py
├── agent/
├── context/
├── infra/
├── runtime/
├── state/
├── tools/
└── remote/
eval/
```

The key point is that AutoCode is no longer just a tiny teaching kernel. It is now a lightweight local runtime with a readable core.

## Commands

```text
/model
/compact
/tokens
/diff
/resume
/task
/todo
/trace
/approve
/approve_all
/reject
/reset
```

## FAQ

**Does AutoCode support Skills / Subagents / MCP?**

Partially.

- Subagents: yes. There is a built-in `agent` tool that spawns a sub-agent with an isolated context.
- MCP and Skills: no native framework yet. Those layers are still intentionally absent.

So the old "minimal core only" description is no longer fully accurate. AutoCode has already grown into a lightweight runtime, but it still stops short of becoming a full plugin / MCP platform.

## License

MIT.
