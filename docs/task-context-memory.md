# 任务上下文：增量 notes 与原始历史召回

本次修改的是 AutoCoder Agent 的上下文管理，不是模型权重。默认工具注册表已启用
`search_history` 和 `read_history`，不需要新增配置或向量数据库。已有进程需要重启才能
加载新代码和工具。2026-09-05 已按用户要求通过 `AutoCodeLocalWebRunner` 计划任务
重启本机 Runner，健康接口返回 `runner_connected: true`；没有部署远程 Relay。
新用户轮次会加载新的工具快照；从旧版本恢复的进行中轮次仍保留原有冻结快照，不在
执行途中修改其工具契约。手动传入自定义 `tools` 列表的调用方应包含这两个历史工具。

## 一次窗口切换

1. `Agent._append_message` 继续将原始用户消息、模型回复和工具结果写入当前会话的
   `transcript.jsonl`，为每条消息保留稳定 ID。
2. `ContextManager.maybe_compress` 在副本上规划输出裁剪和近期消息保留。
3. `Agent._checkpoint_task_context` 确认证据存在，再调用 `TaskNotes.checkpoint`。
   老 checkpoint 中仍在窗口、但未写入 transcript 的消息会先补存；不是空 notes 清窗。
4. 原始日志先刷新落盘，再按游标读取尚未处理的证据。每页最多 8,000 个文本字符，
   按约 16,000 个 JSON 字符分批；超长输出会逐页处理，不会默默截掉中间内容。
5. 模型返回 `upsert` / `remove` 增量。程序校验字段、来源和预算后，将 notes 与游标
   一起写入临时文件，`flush` / `fsync` 后通过 `os.replace` 替换 `task_notes.json`。
6. 保存成功后才替换运行窗口，携带任务 notes 和近期消息继续执行。notes 模型调用或
   保存失败会报错，原来的运行上下文不变；已经成功提交的分批游标可用于重试。

70% 阈值优先保留近期完整用户轮次；90% 阈值支持在长单轮任务中切窗，保留原始用户
提示词与最近完整工具批次。中途追加的 `steer` 不会被误认为新轮次而挤掉可编辑原始提示词。
50% 的工具文本裁剪也必须先完成证据与 notes 保存。

notes 包含目标、有效约束、决定、失败与原因、待验证问题、事实。未变条目不重写。
上限为 20 条、每条文本 240 字符、完整 notes JSON 12,000 字符；一次 upsert 最多引用
8 个来源，更新同一条目会保留原有依据。已结束的细节可退出常驻 notes，原始证据仍可检索。
项目级 `PROJECT_MEMORY.md` 的机制保留，任务状态不会被直接写入该文件。

## 历史工具

| 工具 | 输入与输出 |
| --- | --- |
| `search_history` | 空格分隔关键词，OR 匹配；按命中词数、消息新旧排序。默认 5 条，最多 10 条，每条片段最多 600 字符，包含消息 ID、角色、时间、修订信息及片段位置。 |
| `read_history` | 按消息 ID 读取，默认 4,000 字符，最多 8,000；返回总长度、下一页 offset、前后消息 ID、工具调用 ID。可顺着相邻 ID 找到调用与结果。 |

搜索和普通读取不返回图片 base64。`media_count` 标记图片数量；需要看原图时，使用
`read_history(..., include_media=true)`，通过既有多模态工具结果通道返回图片。
notes 提炼只接收文本及媒体数量，不假装已经理解未提供的图片。

两个工具没有跨会话 ID 参数，始终绑定当前 Agent 的会话。召回结果是历史证据，不是
新的系统指令；旧测试通过不代表当前状态通过。工具描述和切窗 notes 提示模型在缺少
精确错误、准备重试失败方案、遇到冲突证据时主动回查。

历史检索不再索引 `search_history` / `read_history` 的工具输出副本及纯检索调用，避免
“搜索结果再成为搜索结果”。当前实现是对本地 transcript 的关键词扫描，不是语义搜索。

## 编辑、恢复与失败边界

- `turn_superseded` 对应的旧轮次不会出现在正常搜索或读取中。notes 的任一来源被撤回，
  整条派生 notes 就退出有效视图；编辑后和生成模型请求前都会刷新窗口中的 notes。
- 缺少轮次标识的旧格式原始记录，发生编辑后无法确认所属修订，保守地不参与正常召回。
  原文件保留，不会为检索而删除原始历史。
- notes 位于会话目录，跟随既有会话分区及恢复机制，不依赖进程内缓存。
- notes 输出支持纯 JSON 和完整 JSON Markdown 代码块；不接受额外解释文本、未知
  字段、伪造来源、未知删除项或截断输出，不使用空 notes 兜底。
- 不完整的 transcript 末行不进入处理游标；完整但损坏的 JSON 会报错，不静默跳过。
- 这套机制不是无限上下文：超大用户单条输入、图片载荷、固定系统提示词或完整工具
  批次本身过大，仍可能超出模型限制。字符估算不是精确 tokenizer。
- 持久化与检索正确性不等于模型提炼语义永不遗漏；遗漏细节仍需通过原始历史找回。

## 验证与复现

定向测试包括长输出中间细节、分页重组、会话隔离、撤回与派生 notes 失效、两种 provider
消息序列化、增量游标、部分写入失败恢复、原子替换失败、非法/截断模型输出、完整工具
批次、会话恢复后编辑、图片按需读取、召回副本过滤及中途追加指令。

```powershell
& ".venv/Scripts/python.exe" -m pytest -q "tests/test_task_history.py" "tests/test_core.py" "tests/test_transcript.py" "tests/test_tools.py"
& ".venv/Scripts/python.exe" -m pytest -q
& ".venv/Scripts/python.exe" -m eval.task_memory --runs 10
```

真实模型验证读取当前 `.env` 配置，但不打印密钥或地址。使用隔离会话、本地进程生成的
合成诊断日志、只读工具及禁用外部追踪；没有操作业务数据。应用层窗口阈值缩小为
3,000 tokens，使实际样本长度触发两次切窗，不伪造 provider usage，也不改变全局模型配置。

2026-09-05 验证使用 `macaron-v1-coding-venti`、Anthropic Messages：

- 连续 10 轮通过：每轮两次切窗、notes 增量保存、恢复会话、模型调用两个历史工具，
  找回确切错误码、认证前失败阶段，并引用原始消息 ID。
- 最终补充 3 轮通过，增加严格断言：`read_history` 的实际工具输出必须包含目标原始
  消息 ID 和错误码，不能仅凭 notes 中的内容回答。
- 前 10 轮报告：`eval/runs/task-memory-2igkuhuf/report.json`。
- 最终 3 轮报告：`eval/runs/task-memory-i3f7h1h4/report.json`。
- 完整轨迹、notes 及原始模型 notes 输出位于相应报告目录，属于本地 eval 产物，不提交 Git。
- 定向回归为 113 passed，见 `eval/runs/task-memory-focused-final.xml`。
- 按用户要求删除失效的单行提示词断言后，全量回归为 479 passed、1 skipped、3 warnings，
  见 `eval/runs/task-memory-release.xml`。

之前的全量回归发现一项基线问题：
`test_system_prompt_requests_independent_batches_and_rejects_blind_retries` 中断言提示词应包含
`Treat that block as metadata`，而提示词不包含该句。后续按用户明确要求只删除这一行
断言，保留同一测试里的并行调用、避免盲目重试等其他检查，没有删除整个测试函数。

原来的递归总摘要实现及专门针对该旧行为的测试已移除，替换为上述 checkpoint 契约和
回归测试。标准库落盘接口参考 [os.replace](https://docs.python.org/3/library/os.html#os.replace)。
