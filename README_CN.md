# AutoCode

> 原名 **AutoCode**（更早叫 **NanoCoder**），这次统一改为 **AutoCode**，让产品名和 CLI 名称保持一致。


[English](README.md) | [中文](README_CN.md) | [Claude Code 源码深度导读（7 篇）](article/)

[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/AutoCode/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/AutoCode/actions)

**51万行 TypeScript → ~1,400 行 Python。**

我逆向了 Claude Code 泄露的全部源码，然后把不承重的部分全扔掉，用 Python 重建了核心。成果：**Claude Code 的每一个关键架构模式，浓缩在一个下午能读完的代码库里。**

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

Claude Code 51 万行源码提炼出来的 7 个核心模式：

| 设计模式 | Claude Code | AutoCode |
|---|---|---|
| 搜索替换编辑（唯一匹配 + diff） | FileEditTool | `tools/edit.py` — 70 行 |
| 并行工具执行 | StreamingToolExecutor（530行） | `agent.py` — ThreadPool |
| 三层上下文压缩 | HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE | `context.py` — 145 行 |
| 子代理隔离上下文 | AgentTool（1,397行） | `tools/agent.py` — 50 行 |
| 危险命令拦截 | BashTool（1,143行） | `tools/bash.py` — 95 行 |
| 会话持久化 | QueryEngine（1,295行） | `session.py` — 65 行 |
| 动态系统提示词 | prompts.ts（914行） | `prompt.py` — 35 行 |

每个模式都是可运行的实现，不是流程图，不是博客文章。

## 安装

```bash
pip install -e .
```

选你的模型，任何 OpenAI 兼容 API 都行。可以 `export` 环境变量，也可以在项目根目录放一个 `.env` 文件：

```bash
# Kimi K2.5
export OPENAI_API_KEY=你的key OPENAI_BASE_URL=https://api.moonshot.ai/v1
autocode -m kimi-k2.5

# Claude Opus 4.6（通过 OpenRouter）
export OPENAI_API_KEY=你的key OPENAI_BASE_URL=https://openrouter.ai/api/v1
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

# Ollama（本地）
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1
autocode -m qwen3:32b

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
- `/task`、`/tasks`、`/trace`、`/resume`、`/reset` 与 Telegram 保持一致
- `/approve`、`/approve_all`、`/reject` 也支持直接文本输入
- 待审批操作会以交互卡片形式发送，内置 **Approve / Approve All / Reject** 按钮
- 整个适配层直接复用现有任务运行时和审批状态，不再额外造第二套工作流

这样可以在不暴露公网 webhook 的前提下，实现飞书里的完整双向控制，同时保持代码体量可控。

## 架构

整个项目一目了然：

```
autocode/
├── cli.py            REPL + 命令                   218 行
├── agent.py          Agent 循环 + 并行执行          122 行
├── llm.py            流式客户端 + 重试              156 行
├── context.py        三层压缩                       196 行
├── session.py        会话保存/恢复                   68 行
├── prompt.py         系统提示词                      33 行
├── config.py         环境变量配置                    55 行
└── tools/
    ├── bash.py       Shell + 安全 + cd 追踪         115 行
    ├── edit.py       搜索替换 + diff                  85 行
    ├── read.py       文件读取                         53 行
    ├── write.py      文件写入                         36 行
    ├── glob_tool.py  文件搜索                         47 行
    ├── grep.py       内容搜索                         78 行
    └── agent.py      子代理生成                       58 行
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
/save            保存会话
/sessions        列出已保存的会话
/reset           清空历史
quit             退出
```

保存的会话 ID 会先安全化再作为文件名，恢复数据始终留在 `~/.autocode/sessions` 目录内。

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
/tasks           列出最近的 checkpoint
/trace           查看当前任务的 trace
/approve         批准待执行的工具调用
/approve_all     批准当前操作，并自动放行后续普通确认
/reject          拒绝待执行的工具调用
/resume <id>     把保存的任务 checkpoint 恢复到当前聊天
/reset           清空当前聊天对应的内存会话
```

## 对比

|  | Claude Code | Claw-Code | Aider | AutoCode |
|---|---|---|---|---|
| 代码量 | 51万行（闭源） | 10万+行 | 5万+行 | **~1,400 行** |
| 模型 | 仅 Anthropic | 多模型 | 多模型 | **任意 OpenAI 兼容** |
| 能通读吗？ | 不能 | 很难 | 有点费劲 | **一个下午** |
| 适合 | 直接用 | 直接用 | 直接用 | **先看懂，再造自己的** |

## 源码导读

我还写了 [7 篇 Claude Code 架构深度导读](article/)：Agent 循环、工具系统、上下文压缩、流式执行、多 Agent、隐藏功能。想知道 AutoCode 为什么这样设计，从那里开始。

## FAQ

**AutoCode 支持 Skill / Subagent / MCP 吗？**

不支持，这是刻意的。AutoCode 只保留可运行的最小核心 —— agent 循环、工具、流式、压缩。Skill、Subagent、MCP、hook、plugin 都是 Claude Code 在上层加的特性；如果 AutoCode 也全都做了，就不再是一个可读的教学产物。上面的架构导读系列讲了 Claude Code 里这些系统是怎么工作的，你可以照着自己加。

如果你只是想要 Skill，配方很简单：启动时扫 `~/.claude/skills/*.md`，把标题列进 system prompt，让 agent 按名字请求某个 skill，再把那个文件的内容 inline 进对话就行了。

## License

MIT。Fork，然后拿去造更好的东西，如果能标注此出处就更好了。

---

作者 **[何宇峰](https://github.com/he-yufeng)** · Agentic AI Researcher @ Moonshot AI (Kimi)

[Claude Code 源码分析（知乎 17 万阅读，6000收藏）](https://zhuanlan.zhihu.com/p/1898797658343862272)

