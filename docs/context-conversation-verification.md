# 256k 连续真实会话验收与计数修复

## 范围

2026-09-05，模型 macaron-v1-coding-venti，Anthropic Messages 接口。
所有阶段共用 session_20260905_132952_20f835f1、同一份 transcript 和 notes。
任务是只读审查 AutoCoder 的真实源码和测试。模型只获得历史、笔记、切窗和只读快照分页工具，
没有 shell、业务写操作和 Langfuse。不是重新播放完整 Codex 聊天，也不是模拟模型响应。

上下文固定 256,000。初始输出上限 4,096 导致首轮审查回答 max_tokens；保留失败记录后，
从磁盘恢复同一会话，将本次验收输出额度改为 16,384。没有修改日常运行配置。
恢复后的提醒线 206,848、基础预算 223,232、输入硬边界 239,616。

验收驱动：eval/context_conversation.py。来源路径、SHA256、完整源码快照随报告保存。
分页尺寸用于控制单次读取量，不修改生产阈值，不伪造 provider usage，不使用重复填充文本。
修复前分页参考了较大的正文估算，靠近该值时采用小页，导致较多重复输入请求；
修复后分页仅参考 Agent 的占用估算，因此两轮不能用于性能/成本的公平比较。

## 连续会话的观测

1. 第一阶段用户只要求代码审查，没有要求记笔记。正常完成后 notes 仍为空。
   不能声称系统提示词已确保模型在低压力阶段主动维护笔记。
2. 扩展到全部源码后，13:35:46 发生硬切窗：126 条消息变为一个恢复入口，notes 为空，
   此前没有预算提醒，也没有模型 new_context 调用。
   最后实际输入为 173,900，正文字符估算为 240,063，后者越过了 239,616。
3. 模型在新窗口通过 list_notes/list_history/read_history 恢复，之后才主动保存笔记，
   继续读完全部源码。历史回查可用，但不是理想的“先保存再主动切窗”。
4. 用户明确要求更新笔记后，模型先读原笔记，write_note/append_note 保存实际约束，
   new_context 切窗，随后 read_note/read_history 恢复并引用历史。
5. 用户授权修正计数后，从同一会话继续复审修复后的完整源码，未在新用户要求中命令记笔记或切窗。
   请求 103 的正文估算已达 264,732，但 Agent 估算仅 205,652，不再被错误硬切窗。
6. 13:41:40 程序发出预算提醒，基础预算剩余 15,247。提醒加入后的下一请求估算为 208,046。
   模型在请求 104 写 revised_findings.md，请求 105 写 index.md，请求 106 调用 new_context。
   笔记记录已读 offset 753930、剩余 357640 字符，以及尚待核对的测试和真实约束。
7. 13:42:13 主动切窗，48 条消息变为恢复入口。随后模型读取 index.md、约束文件、
   revised_findings.md 等，继续 next_review_sources，直到 remaining_chars=0。

原始报告：eval/runs/context-conversation-lc9u3hz6/report.json。
其中 commit 字段为初始基线；修复后源码由 recheck-source-manifest.json 中逐文件 SHA256 固定。
观察汇总：同目录 observations.json。完整对话位于同目录 sessions 下的 transcript.jsonl。
报告中的请求序号、工具参数、笔记快照和源码游标可以互相核对。
累计输入应按 requests[*].prompt_tokens 求和（包括恢复前请求），不是窗口占用，也不是价格计算。
切窗事件的 after_tokens 是恢复入口正文估算，不包含系统提示和工具定义，不能当作完整请求大小。

## 根因与修复

Agent._estimated_context_tokens 已正确执行：有效实测锚点 + 锚点后新增消息估算；
锚点无效时才估算当前消息及可用的系统/工具快照。
错误在下游 ContextManager.effective_used 再取 max(全量字符估算, Agent 估算)，
而提醒路径仍直接使用 Agent 估算，造成提醒和硬切窗使用两个不同的占用口径。

删除 effective_used。maybe_compress 接收明确的 used_tokens，Agent 只计算一次并同时用于
切窗和提醒；独立调用 ContextManager 未提供占用时才自行估算。旧参数接口不保留兼容分支。
新增回归覆盖低于提醒、提醒线、基础线、硬边界，以及修改消息后锚点失效的场景。
4 个针对该错判的用例在旧实现上先失败，再在修复后通过。

官方复核基准仍为 openai/codex@459a79eb85400af759e9220c7bafb4429ae07516：
core/src/session/context_window.rs 从同一 active_context_tokens 推导剩余预算和完整窗口硬边界；
core/src/session/token_budget.rs 按基础窗口剩余量发每窗提醒。此次核对不等于已经证明
AutoCoder 复制了官方底层 usage 估算器或 Astra 专有模型行为。
官方配置文档也明确实验性机制使用 notes + searchable history，而非反复合并单一摘要：
https://learn.chatgpt.com/docs/config-file/config-reference

## 结论边界

- 修复后的“预算提醒 → 模型保存笔记 → 主动切窗 → 读取笔记 → 继续任务”有真实执行证据。
- 明确要求更新笔记的路径在同一会话中通过。
- 低压力阶段的主动笔记没有观察到；恢复后出现主动写入，不代表最初阶段也成功。
- 本次只是一条连续会话，不是多个独立样本，不能推出普遍成功率。
- 模型生成的审查结论/笔记仍可能有错误；存储成功不代表其中每项代码判断已经验证。
- 256k 是应用测试窗口；实际模型请求在主动切窗前最高约 207k，未声称顶满 256k。

## 回归验证

- 全量：487 passed、1 skipped、3 warnings，耗时 197.98 秒。
  JUnit 报告：eval/runs/context-conversation-regression.xml。
- 最终相关模块与验收驱动测试：74 passed，耗时 4.85 秒；与全量有重叠，不相加。
- compileall、git diff --check 通过。
- 跳过项是 Windows 符号链接权限测试；3 项为既有依赖弃用警告。
- 单元测试通过不替代行为验收：主动笔记未观察到的结果仍保留，不因测试绿色而改判。

## 复现

```powershell
& ".venv/Scripts/python.exe" -m eval.context_conversation
& ".venv/Scripts/python.exe" -m eval.context_conversation --resume "eval/runs/<目录>" --recheck-budget
& ".venv/Scripts/python.exe" -m eval.context_conversation --analyze "eval/runs/<目录>"
```

第二条命令必须使用原会话报告目录；它继续已有会话并重新开放当前源码快照，不创建另一个任务。
