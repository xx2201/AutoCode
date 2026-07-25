# AutoCode

> 原名 **AutoCode**（更早叫 **NanoCoder**），这次统一改为 **AutoCode**，让产品名和 CLI 名称保持一致。


[English](README.md) | [中文](README_CN.md) | [Claude Code 源码深度导读（7 篇）](article/)

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/AutoCode/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/AutoCode/actions)

**把 Claude Code 的关键模式，重建成一个轻量级本地 coding agent runtime。**

我逆向了 Claude Code 泄露的全部源码，然后把不承重的部分全扔掉，用 Python 重建核心思想。现在它依然足够紧凑、可以直接通读，但已经不再只是一个玩具级 loop，而是演化成了一个**可本地使用的运行时**：带审批、checkpoint、trace、远程适配层，以及本地评测系统。

AutoCode 不仅是一个 AI 编程工具。它是一份**蓝图**，编程 Agent 领域的 [nanoGPT](https://github.com/karpathy/nanoGPT)。读懂它，fork 它，然后造你自己的。

---

```
$ autocode -m kimi-k2.5

You > 读一下 main.py，修掉拼错的 import

  > read_file(file_path='main.py')
  > edit_file(file_path='main.py', ...)

--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-from utils import halper
+from utils import helper

修好了：halper → helper。
```

## 你能得到什么

AutoCode 现在同时提供两层价值：

- 一个可读、可 fork、可二次开发的 coding-agent 核心
- 一个带安全、恢复、可观测、远程控制、评测能力的本地 runtime

它保留的 Claude Code 核心模式仍然是这些：

| 设计模式 | Claude Code | AutoCode |
|---|---|---|
| 搜索替换编辑（唯一匹配 + diff） | FileEditTool | `autocode/tools/edit.py` |
| 并行工具执行 | StreamingToolExecutor | `autocode/runtime/engine.py` |
| 三层上下文压缩 | HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE | `autocode/context/manager.py` |
| 子代理隔离上下文 | AgentTool | `autocode/tools/agent.py` |
| 危险命令拦截 | BashTool | `autocode/runtime/policy.py` + `autocode/tools/bash.py` |
| 会话恢复 + 任务状态 | QueryEngine 风格运行态 | `autocode/state/` |
| 动态系统提示词 | prompts.ts | `autocode/context/prompt.py` |

每个模式都是可运行的实现，不是流程图，不是博客文章。

和最初“极简内核”相比，现在真实情况是：

- 仓库已经不只是一个很小的单包 demo
- `autocode/` 已经按 `agent / context / infra / runtime / state / tools / remote` 分层
- 除了核心 agent loop，仓库还包含 Telegram / 飞书远程控制，以及独立的本地评测系统 `eval/`

## 安装

```bash
pip install -e .
```

选你的模型。默认读取的是 `AUTOCODE_*` 环境变量，并连接任意 OpenAI-compatible 接口。可以 `export` 环境变量，也可以在项目根目录放一个 `.env` 文件：

```bash
# Kimi K2.5
export AUTOCODE_API_KEY=你的key AUTOCODE_BASE_URL=https://api.moonshot.ai/v1
export AUTOCODE_MODEL=kimi-k2.5
autocode

# Claude Opus 4.6（通过 OpenRouter）
export AUTOCODE_API_KEY=你的key AUTOCODE_BASE_URL=https://openrouter.ai/api/v1
export AUTOCODE_MODEL=anthropic/claude-opus-4-6
autocode

# OpenAI GPT-5
export AUTOCODE_API_KEY=sk-...
export AUTOCODE_MODEL=gpt-5
autocode

# DeepSeek V3
export AUTOCODE_API_KEY=sk-... AUTOCODE_BASE_URL=https://api.deepseek.com
export AUTOCODE_MODEL=deepseek-chat
autocode

# Qwen 3.5
export AUTOCODE_API_KEY=sk-... AUTOCODE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export AUTOCODE_MODEL=qwen-max
autocode

# Ollama（本地）
export AUTOCODE_API_KEY=ollama AUTOCODE_BASE_URL=http://localhost:11434/v1
export AUTOCODE_MODEL=qwen3:32b
autocode

# 单次模式
autocode -p "给 parse_config() 加上错误处理"
```

### Telegram 手机控制（实验性）

如果你想在手机上控制 AutoCode，可以安装可选的 Telegram 适配层：

```bash
pip install -e '.[telegram]'

export AUTOCODE_MODEL=gpt-5
export AUTOCODE_API_KEY=sk-...
export AUTOCODE_TELEGRAM_BOT_TOKEN=123456:telegram-token
export AUTOCODE_TELEGRAM_ALLOWED_CHATS=123456789
autocode-telegram
```

这个 Telegram 控制层刻意保持极简，直接复用现有任务运行时：

- 发送任意文本即可启动或继续代码任务
- `/task` 查看当前任务状态
- `/trace` 查看最近一次 trace 摘要
- `/approve` / `/reject` 处理待审批操作
- `/approve_all` 批准当前操作，并对同一任务后续的普通确认自动放行
- `/resume <task_id>` 把保存的 checkpoint 恢复到当前聊天

实现思路参考了 `claw0` 的 channel adapter 模式，但仍保持 AutoCode 单通道、轻量级，不把项目扩成完整 agent gateway。

### 飞书手机控制（实验性）

如果你想在飞书里远程控制 AutoCode，可以安装可选的飞书适配层：

```bash
pip install -e '.[feishu]'

export AUTOCODE_MODEL=gpt-5
export AUTOCODE_API_KEY=sk-...
export AUTOCODE_FEISHU_APP_ID=cli_xxx
export AUTOCODE_FEISHU_APP_SECRET=xxx
# 可选白名单
export AUTOCODE_FEISHU_ALLOWED_OPEN_IDS=ou_xxx
export AUTOCODE_FEISHU_ALLOWED_CHAT_IDS=oc_xxx
autocode-feishu
```

这个飞书控制层采用官方的“应用机器人 + 长连接事件”模式：

- 发送任意文本即可启动或继续代码任务
- `/task`、`/trace`、`/resume`、`/reset` 支持直接文本输入
- `/approve`、`/approve_all`、`/reject` 也支持直接文本输入
- `/resume` 会以交互卡片列出当前项目最近可恢复会话
- 待审批操作会以交互卡片形式发送，内置 **Approve / Approve All / Reject** 按钮
- 整个适配层直接复用现有任务运行时和审批状态，不再额外造第二套工作流

这样可以在不暴露公网 webhook 的前提下，实现飞书里的完整双向控制，同时保持代码体量可控。

## 架构

Web Relay、本地 Workspace、多模态输入、Langfuse 观测以及本地状态文件的完整边界，
见 [架构说明](docs/architecture.md)。

现在的仓库依然不大，但已经不是最初那种单文件教学内核：

```
autocode/
├── cli.py              REPL + 本地命令
├── llm.py              OpenAI-compatible / LiteLLM 客户端
├── config.py           环境变量与工作区配置
├── agent/
│   └── loop.py         主 agent 循环
├── context/
│   ├── manager.py      三层上下文压缩
│   ├── prompt.py       动态系统提示词
│   └── memory.py       项目记忆管理
├── infra/
│   ├── filesystem.py   工作区边界文件系统
│   ├── sandbox.py      Shell 执行
│   └── processes.py    受控后台进程
├── runtime/
│   ├── engine.py       LLM / tool 执行运行时
│   ├── policy.py       审批与安全策略
│   └── hooks.py        事件总线
├── state/
│   ├── checkpoint.py   会话/任务持久化
│   ├── trace.py        trace 聚合
│   └── transcript.py   原始消息日志
├── tools/
│   ├── read.py / write.py / edit.py / grep.py / glob_tool.py
│   ├── bash.py         shell 工具
│   ├── process.py      后台进程 start/read/wait/stop
│   ├── todo_write.py   显式计划状态
│   └── agent.py        子 agent 工具
└── remote/
    ├── telegram_bot.py
    ├── feishu_bot.py
    └── manager.py
eval/
└── ...                 本地评测 harness
```

## 当库用

```python
from autocode import Agent, LLM

llm = LLM(model="kimi-k2.5", api_key="your-key", base_url="https://api.moonshot.ai/v1")
agent = Agent(llm=llm)
response = agent.chat("找出项目里所有 TODO 注释并列出来")
```

## 加自定义工具（约 20 行）

```python
from autocode.tools.base import Tool

class HttpTool(Tool):
    name = "http"
    description = "请求一个 URL。"
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def execute(self, url: str) -> str:
        import urllib.request
        return urllib.request.urlopen(url).read().decode()[:5000]
```

## 命令

```
/model           查看当前模型
/model <名称>    切换模型
/compact         压缩上下文（对标 Claude Code 的 /compact）
/tokens          查看 token 用量 + 费用估算
/diff            查看本次会话修改的文件
/resume          列出当前项目可恢复会话
/resume <id>     按 id 恢复会话
/task            查看当前任务状态
/todo            查看当前 todo 列表
/trace           查看当前会话 trace
/approve         批准待执行的工具调用
/approve_all     批准当前操作，并自动放行后续普通确认
/reject          拒绝待执行的工具调用
/reset           清空历史
quit             退出
```

Telegram 命令：

```
/start           显示 Telegram 帮助
/task            查看当前任务状态
/tasks           列出最近的 checkpoint
/trace           查看当前任务的 trace
/approve         批准待执行的工具调用
/approve_all     批准当前操作，并自动放行后续普通确认
/reject          拒绝待执行的工具调用
/resume <id>     把保存的任务 checkpoint 恢复到当前聊天
/reset           清空当前聊天对应的内存会话
```

飞书命令：

```
/start           显示飞书帮助
/task            查看当前任务状态
/trace           查看当前任务的 trace
/approve         批准待执行的工具调用
/approve_all     批准当前操作，并自动放行后续普通确认
/reject          拒绝待执行的工具调用
/resume          列出当前项目可恢复会话
/reset           清空当前聊天对应的内存会话
```

## 对比

|  | Claude Code | Claw-Code | Aider | AutoCode |
|---|---|---|---|---|
| 代码量 | 51万行（闭源） | 10万+行 | 5万+行 | **5k+ 行核心包** |
| 模型 | 仅 Anthropic | 多模型 | 多模型 | **任意 OpenAI 兼容** |
| 能通读吗？ | 不能 | 很难 | 有点费劲 | **一个下午** |
| 适合 | 直接用 | 直接用 | 直接用 | **先看懂，再造自己的** |

## 源码导读

我还写了 [7 篇 Claude Code 架构深度导读](article/)：Agent 循环、工具系统、上下文压缩、流式执行、多 Agent、隐藏功能。想知道 AutoCode 为什么这样设计，从那里开始。

## FAQ

**AutoCode 支持 Skill / Subagent / MCP 吗？**

部分支持。

- Subagent：支持。内置了 `agent` 工具，会生成一个隔离上下文的子 agent。
- MCP 和 Skills：还不支持原生框架，这两层目前仍然刻意缺席。

所以旧版 README 里“只保留最小核心、不支持 Subagent”的说法，已经和当前实现不完全一致。更准确地说，AutoCode 已经长成了一个轻量 runtime，但还没有继续扩成完整的 plugin / MCP 平台。

如果你只是想要 Skill，配方很简单：启动时扫 `~/.claude/skills/*.md`，把标题列进 system prompt，让 agent 按名字请求某个 skill，再把那个文件的内容 inline 进对话就行了。

## 开发机部署

开发机 Relay 使用版本化 wheel 发布目录，不是 Git 工作树。更新服务器前请先
阅读[开发机部署手册](docs/development-deployment.md)，不要在
`/home/dev/corecoder-web` 中执行 `git pull`。

## License

MIT。Fork，然后拿去造更好的东西，如果能标注此出处就更好了。

---

作者 **[何宇峰](https://github.com/he-yufeng)** · Agentic AI Researcher @ Moonshot AI (Kimi)

[Claude Code 源码分析（知乎 17 万阅读，6000收藏）](https://zhuanlan.zhihu.com/p/1898797658343862272)

