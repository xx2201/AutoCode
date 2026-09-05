# 任务上下文：模型管理 notes 与无摘要切窗

本次对齐 Codex 公开的 token-budget/new_context 生命周期，不修改模型权重，不接入其专有后端。
不实现 embedding 或混合检索。官方源码基准为 `openai/codex@459a79eb85400af759e9220c7bafb4429ae07516`。

## 主链路

1. 原始消息与工具结果先追加到 transcript.jsonl，保留 message_id、turn/revision、window_id。
2. 模型在任务过程中使用 write_note / append_note 保存笔记。建议 index.md 指向详细文件。
   没有固定的条目数、240 字符字段、8 个来源限制，也不再调用 delta 摘要模型。
3. ContextManager.status 使用 token 预算给出提醒、准备 buffer 和硬边界。
   每个窗口的提醒与准备提示只发送一次，标志随 SessionState 保存和恢复。
4. new_context 只设置请求标志。所有工具结果提交后，Agent 在批次边界执行窗口转换。
   手动 compact_context 和达到硬边界也进入同一无摘要转换流程。
5. 转换前同步原始历史，验证笔记存储可读取；失败保留原窗口。成功后只安装恢复入口，
   清理旧 usage 锚点，递增窗口编号；不保留滚动摘要，不重置工作区、进程和任务。
6. 模型用 list_notes/read_note 恢复状态，用 list_history/search_history/read_history 找回遗漏证据。
   项目记忆功能保留，但切窗不再触发项目记忆的自动摘要。

## 预算含义与官方差异

- 总窗口为 AUTOCODE_MAX_CONTEXT；最大输出为 AUTOCODE_MAX_TOKENS。
- 输入硬边界 = 总窗口 - 最大输出。基础预算 = 输入硬边界 - 准备 buffer。
- 无官方模型目录参数时，默认用一次最大输出额度作为准备 buffer 和提前提醒额度；
  buffer 不能占满输入窗口。ContextManager 可显式传 reminder_tokens/fallback_buffer_tokens。
  这是通用 provider 的本地适配，**不是 Astra 官方默认参数**。
- 50%/70%/90% 裁剪策略已移除；提醒不会裁剪消息。
- 使用已有 provider usage 锚点加新增消息估算；没有有效锚点时估算消息、system 和工具定义。
  本地字符估算不是精确 tokenizer，多模态和超大单次输入仍存在超窗风险。
- 工具文本输出预算由当前剩余输入空间和最大输出额度决定。大段读取返回 next_offset；
  列表/搜索结果过大返回明确错误，要求缩小 limit 或先切窗，不损坏 JSON 或静默吞掉结果。

## 笔记和历史工具

- write_note(path, text, sources?)：完整覆盖虚拟文件；append_note 追加原文。
- read_note：全读或字符分页，也支持 inclusive 1-based 行范围，负数从末尾计。
- list_notes / search_notes：前缀、分页；笔记搜索为区分大小写的子串匹配。
- new_context：提交后切窗，不执行额外摘要请求。
- list_history：按窗口、角色、工具筛选，分页返回原始消息 ID。
- search_history：保留现有关键词 OR 匹配、命中数量及新旧排序；不是向量搜索，也不是官方
  的区分大小写子串接口。默认返回 5 条、片段 600 字符；这些是已有检索策略，不是切窗阈值。
- read_history：按 ID 读取原文、相邻 ID、调用 ID、下一页位置，可显式请求图片。

所有工具只访问所属会话。notes 路径是 JSON 存储中的虚拟键，不直接解释为 Windows 文件路径。
禁止空、绝对、反斜杠和父目录路径。每个虚拟文件上限 1,000,000 UTF-8 字节，
对应官方公开的单文件契约，超限创建另一个文件；不是整个任务记忆的总上限。
实际保存为会话目录 notes.json，通过临时文件、flush/fsync、os.replace 原子更新。
旧 task_notes.json 在首次读取时单向迁移到 migrated/*.md，旧文件不删除；不保留旧压缩执行逻辑。

撤回轮次后，原始消息不再参加正常召回。笔记记录显式来源，并保守记录写入时可见轮次；
任一依赖轮次被撤回，整份笔记退出有效视图。自由文本无法验证完备来源，因此这个保守策略
可能使无关笔记也失效，须从有效历史重新构建；不会把撤回的约束重新带回。
从已切窗会话编辑最近提问时，通过原始 transcript 定位旧提示，不要求它仍在当前窗口。

## 官方代码复核映射

基准链接：https://github.com/openai/codex/tree/459a79eb85400af759e9220c7bafb4429ae07516

| 官方实现 | 本地实现 | 核对边界 |
| --- | --- | --- |
| core/src/session/context_window.rs | context/manager.py:status | 基础预算、buffer、硬边界；本地只使用总输入范围 |
| core/src/session/token_budget.rs | agent/loop.py:_maybe_compress_messages | 每窗提醒与准备提示；本地没有官方模型目录默认值 |
| core/src/tools/handlers/new_context_window.rs | tools/notes.py:NewContextTool | 工具只请求转换，不直接清空正在执行的批次 |
| core/src/compact_token_budget.rs | ContextManager.maybe_compress | 手动、工具、硬边界切窗均不生成摘要 |
| ext/history-notes/src/tools.rs | tools/notes.py、tools/history.py | 文件式笔记、原文读取；本地扁平工具名、单会话范围 |
| ext/history-notes/src/extension.rs | TaskNotes.render | 恢复入口而非全量笔记；本地固定导航，不复现未公开的 thread_hint 生成器 |

官方模型专有提示、加密内容、远程后端、跨 agent 共享、最终一致性并未复制。
本地工具读取笔记采用独占调度，防止迁移/读改写竞争；写笔记和切窗请求不会在流式模型响应提交前执行。
已有进行中轮次仍使用冻结的工具快照，新用户轮次加载新增工具；升级后应启动新轮次使用完整能力。

## 验证

```powershell
& ".venv/Scripts/python.exe" -m pytest -q
& ".venv/Scripts/python.exe" -m eval.task_memory --runs 10
```

eval 使用配置中的真实 provider/model，最大输出明确设为 4096，只有合成诊断与会话笔记工具，
不启用业务写操作或 Langfuse。模型必须写笔记、调用 new_context、读笔记、搜索并读取历史。
第一次切窗的笔记快照不得包含错误码；恢复成功后模型可以把已找到的证据补入笔记。
随后加入另一条模型尚未处理、notes 中不存在的合成错误，用填充触发应用层硬边界，
保存、恢复后再次回查这条新证据。列表定位与关键词定位都属于有效检索路径。
这是显式引导的工具契约验证，不等于真实长任务中模型会自主选择最佳记忆/切窗时机。
报告位于 eval/runs/context-windows-*/report.json；旧 task-memory-* 报告只适用于旧实现。

### 2026-09-05 实测结果

- 全量回归：477 passed、1 skipped、3 warnings，报告 context-windows-final.xml。
  跳过项是 Windows 创建符号链接需要额外权限。依赖弃用警告未在本次修改中处理。
- 后补的批次顺序测试包含两种 provider 序列化；生命周期测试单独重跑 20 passed，
  报告 context-windows-lifecycle.xml（与全量测试有重叠，不相加）。
- macaron-v1-coding-venti / Anthropic Messages，连续 10 次通过：
  eval/runs/context-windows-f3imcuu7/report.json。每次由模型写 notes、主动切窗、读取原文，
  再由程序强制切窗并恢复，找到 notes 中没有的第二条证据。
- 最终补充 1 次通过：eval/runs/context-windows-t2zwzt0i/report.json。
  强制切窗边界改为当前占用加 3000；实测填充前 5428、输入边界 8428，
  加入合成填充后才越界，没有伪造 provider usage。脚本现使用这个更严格的边界判据。
- 前期脚本把“通过列表定位后读取”错误判成必须使用关键词搜索，且未区分恢复前后笔记；
  已修正验收判据并重新运行。另一次脚本 hook 参数数量错误已修正，失败报告保留用于追溯。
- git diff --check、目标模块 compileall 通过。交付流程要求提交相关代码、重启本机 Runner，
  并核验新进程与 Relay 健康状态；远程部署和 git push 不包含在此次范围内。
