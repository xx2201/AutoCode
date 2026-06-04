# PROJECT_CHALLENGES

### C001：工具注册表与执行链路不一致
- 场景：Agent 初始化时允许注入自定义工具，并基于注入工具生成 tool schema。
- 问题：模型可以看到自定义工具，但实际执行阶段无法命中对应实现。
- 根因：`Agent._exec_tool()` 通过全局 `get_tool()` 查询 `ALL_TOOLS`，没有使用实例级 `self.tools`。
- 方案：后续将工具查询改为实例级 registry，初始化时完成 name -> tool 映射，并统一 schema 生成与执行来源。
- 取舍：全局 registry 实现简单，但会破坏可扩展性与测试隔离；实例级 registry 增加少量初始化复杂度，但边界更清晰。
- 验证：已通过最小复现脚本验证，自定义工具 schema 可生成，但执行返回 `Error: unknown tool`。
- 面试表达：为了让 Agent 支持插件化扩展，我重点处理了“声明可见但运行不可达”的注册一致性问题，把工具系统从全局单例重构为实例级注册中心。
- 更新时间：2026-06-02

### C002：本地 `.env` 与系统环境变量冲突导致鉴权异常
- 场景：项目根目录新增 `.env` 后，使用中转站模型运行 `corecoder`。
- 问题：直接 HTTP 与 OpenAI SDK 最小样例均可成功调用，但 `corecoder` 主程序持续返回 `401 Invalid API key`。
- 根因：`Config.from_env()` 中 `load_dotenv(..., override=False)` 不会覆盖已有系统环境变量，而当前终端已存在旧的 `OPENAI_API_KEY`，导致程序优先读取了错误密钥。
- 方案：改用 `CORECODER_API_KEY` 与 `CORECODER_BASE_URL` 作为项目级独立配置，避开全局 `OPENAI_*` 变量污染。
- 取舍：继续复用 `OPENAI_*` 兼容性更强，但容易受用户全局环境影响；改用 `CORECODER_*` 可显著提升项目隔离性与可诊断性。
- 验证：更新 `.env` 后，`python -m corecoder -p "Reply with exactly: OK"` 成功返回模型结果。
- 面试表达：我处理过一个典型的配置污染问题，同一把 key 在直连脚本能用、主程序却 401，最后通过对环境变量优先级和 dotenv 行为排查，定位到全局变量覆盖，并用命名空间化配置彻底解决。
- 更新时间：2026-06-02

### C003：审批态与 Agent 主循环的状态一致性
- 场景：给极简 Agent 增加权限审批与任务恢复能力，同时保持原有 REPL/one-shot 交互模型不明显变重。
- 问题：一旦工具调用需要人工确认，Agent 会从“连续自动执行”切换到“等待外部输入”，如果仍然只用 messages 作为状态源，就无法在中断后准确恢复。
- 根因：原实现把对话历史当作唯一状态，没有单独建模任务状态、挂起中的 tool call 和 step 进度。
- 方案：新增 `TaskState + PendingApproval + checkpoint`，把审批中的 tool call 提升为显式运行态；主循环在进入等待审批前先落 checkpoint，审批后从挂起点继续执行。
- 取舍：完整状态机会增加少量代码，但能避免把审批逻辑硬塞进 prompt 或 messages；第一版只恢复到“最近完整 step”，不做工具执行中间态恢复，以控制复杂度。
- 验证：新增测试覆盖等待审批、批准继续、checkpoint round-trip，并通过 `76 passed` 与真实 one-shot 调用验证主链路。
- 面试表达：我把一个教学型 Agent 改成了可控执行器，关键不是多加工具，而是把“等待审批”建模成显式状态，并让它可以 checkpoint/resume。
- 更新时间：2026-06-02

### C004：让极简 Agent 具备可追踪性而不污染主循环
- 场景：在保持代码量克制的前提下，为任务执行补齐最小审计日志和 trace 指标。
- 问题：如果把日志统计直接写进 `agent.py` 和各个 tool，会快速让主循环膨胀，并且后续扩展 memory/eval 时很难复用。
- 根因：原项目只有 messages 和控制台输出，没有统一事件流；执行事实、指标统计、恢复状态三者混在一起会导致职责失焦。
- 方案：复用 `HookBus` 建立轻量事件流，新增 `AuditLogger` 负责落 `audit.jsonl`，新增 `TraceRecorder` 负责把同一批事件聚合成 `trace.json`。
- 取舍：事件总线会引入一层间接性，但相比把日志硬编码到主循环里，更利于后续扩展审计、评测和长期记忆；当前只做本地文件输出，不接外部观测平台。
- 验证：新增测试覆盖 trace 聚合、audit 追加与 agent 真实执行后的输出文件生成；全量测试通过 `80 passed`，真实任务目录已生成 `trace.json` 与 `audit.jsonl`。
- 面试表达：为了保持 Agent 内核简洁，我把执行日志设计成旁路事件消费者，让审计和指标统计共享同一条事件流，而不是侵入每个工具和主循环分支。
- 更新时间：2026-06-02

### C005：把散落的文件/命令能力收敛成运行时边界层
- 场景：项目继续补 Todo、Memory、Task、Recovery 后，原先各个 tool 自己处理路径和 shell 执行的方式开始难以扩展。
- 问题：如果 `read/write/edit/grep/glob/bash` 继续各自处理路径解析、cwd、环境变量和边界校验，后续再接权限、审计、worktree 或记忆文件时会出现大量重复逻辑。
- 根因：最初实现为了极简直接把读写和执行写进 tool，本质上只有“工具能力”，没有独立的文件系统层和沙箱层。
- 方案：新增 `WorkspaceFS` 统一路径解析与 workspace 边界，新增 `Sandbox` 统一 shell 执行、cwd 跟踪、环境变量透传和输出截断；各工具只保留能力语义。
- 取舍：抽出两层会多几个文件，但比继续在每个 tool 里堆判断更稳定；当前仍然坚持本地轻量沙箱，不引入容器或虚拟机。
- 验证：新增测试覆盖 workspace 边界、sandbox cwd 跟踪，并保持现有工具测试全部通过；全量测试通过 `87 passed`。
- 面试表达：我把教学型 Agent 的“文件读写 + shell 执行”升级成了显式运行时边界，让后续权限、记忆、任务和审计都建立在统一抽象上，而不是散落在各个工具里。
- 更新时间：2026-06-02

### C006：让静态 Prompt 进化为围绕任务状态的动态上下文组装
- 场景：要走 Claude Code 风格路线时，Agent 不能只靠一段固定 system prompt，还需要感知项目规则、当前 Todo、任务状态和最近失败信息。
- 问题：如果把这些状态直接塞进 messages，会污染对话历史；如果完全不注入，模型又看不到长期规则和当前执行计划。
- 根因：原项目的 prompt 只依赖工具列表和环境信息，没有和 Memory、Todo、Task、Recovery 这些运行态建立连接。
- 方案：把 `system_prompt()` 改成动态组装函数，按轮注入 `memory_block + task_block + todo_block + recovery_block`；同时新增 `todo_write` 工具，让模型显式维护计划。
- 取舍：动态 prompt 会增加少量每轮组装开销，但能显著提升长期规则、一致性计划和错误恢复的可见性；第一版不做向量检索，只读项目文件和最近任务摘要。
- 验证：新增测试覆盖 Todo 更新、Memory 读取、Task 持久化；真实 one-shot 调用和全量测试均通过。
- 面试表达：我没有把记忆系统做成重型知识库，而是围绕任务执行闭环设计了动态 prompt，把规则、计划、状态和失败经验变成模型每轮都能看到的结构化上下文。
- 更新时间：2026-06-02

### C007：Agent 评估不能只看最终答案
- 场景：项目逐步具备 Todo、Policy、Trace、Audit、Recovery 等运行时能力后，需要一套能真正反映 agent 质量的评估体系。
- 问题：如果只看最终输出是否正确，会漏掉大量关键问题，例如危险工具被错误执行、轨迹不合理、没有先读后改、失败后盲目重试，以及 token/step 成本失控。
- 根因：Agent 与普通单轮 LLM 最大差异在于它有“执行轨迹”，而不是只有最终文本；因此结果正确不等于行为正确。
- 方案：在仓库根目录下新增独立 `eval/` 子系统，把评估拆成 Outcome、Trajectory、Safety、Recovery、Efficiency 五类 grader，并复用 `trace.json + audit.jsonl + task.json` 作为评估事实源。
- 取舍：评估系统独立于主项目会增加一些目录和样例 fixture，但能避免把评测逻辑硬塞进 agent 内核，同时方便后续扩展更多任务与报告格式。
- 验证：新增 `eval/` 任务定义、fixture、runner、report 和单元测试；全量测试通过 `90 passed`，`python -m eval.runner --list` 可正常列出任务，真实 smoke trial 可生成 `summary.json` 与 `report.md`。
- 面试表达：我没有只做“能跑的 agent”，而是单独做了一套本地评估 harness，用轨迹、安全和效率去衡量 agent，这比只看最终答案更接近真实工程质量。
- 更新时间：2026-06-02

### C008：让规则评估和 LLM Judge 在同一套 Harness 内共存
- 场景：独立 `eval/` 子系统已经能做 Outcome、Trajectory、Safety、Recovery、Efficiency 五类规则评估，但开放式任务仍缺少“整体质量”判断。
- 问题：如果只加 LLM judge，回归基线会变得波动且昂贵；如果只保留规则 grader，又无法评价计划质量、解释质量和整体执行合理性。
- 根因：规则 grader 适合硬约束，LLM judge 适合软判断，两者评估对象不同，不能简单替代。
- 方案：新增 `eval/judge.py`，把 LLM judge 设计成“可选的第六类 grader”，默认继续保留规则 grader 作为基线；运行模型和评估模型通过独立环境变量解耦。
- 取舍：双层评估会增加一点配置复杂度，但能同时兼顾稳定回归与开放质量判断；当前仍然以规则 grader 为主，不让 LLM judge 决定唯一真相。
- 验证：新增 judge schema、CLI 参数、报告元数据和单元测试；后续可直接用 `qwen-max` 作为评估模型运行真实 smoke eval。
- 面试表达：我把 agent eval 设计成“规则评估打底，LLM judge 补充”的双层结构，这样既能做稳定回归，也能衡量开放式任务的整体质量。
- 更新时间：2026-06-03

### C009：题库扩充后，grader 不能因为“目标文件尚未创建”而崩溃
- 场景：给 `eval/tasks/` 新增“缺失模块创建”一类任务时，某些期望文件在 trial 开始前并不存在。
- 问题：如果 agent 没有成功创建文件，规则 grader 会在 `read_text()` 时报错，导致整次评估直接异常退出，而不是把任务判失败。
- 根因：最初的 Outcome grader 默认所有 `must_contain` / `must_not_contain` 文件都已存在，更适合“编辑已有文件”场景，不适合扩展到创建型任务。
- 方案：把文件内容读取改成安全读取，目标文件不存在时返回失败检查而不是抛异常。
- 取舍：这样做会少掉一部分底层异常堆栈信息，但评估系统更健壮，也更适合批量跑题库；真正的调试信息仍然可以从 `audit.jsonl` 和 `trace.json` 查看。
- 验证：新增创建型任务后，grader 可稳定输出 fail/pass，不再因为文件缺失中断整轮 eval。
- 面试表达：我在扩展 agent benchmark 时专门处理了“创建型任务”和“编辑型任务”的 grader 假设差异，让评估系统从只适配已有文件，升级成能稳定评估完整开发闭环。
- 更新时间：2026-06-03

### C010：运行模型与评估模型的环境变量必须彻底隔离
- 场景：项目同时支持主 agent 运行模型和 `qwen-max` 评估模型，二者都走 OpenAI-compatible SDK。
- 问题：如果主链路继续回退读取 `OPENAI_API_KEY / OPENAI_BASE_URL`，评估模型的环境变量就可能串进 agent 运行，表现为启动正常、实际请求却拿错 key。
- 根因：主运行时和评估时共用了一套过于宽松的环境变量回退策略，导致“谁先占到 `OPENAI_*`”就可能污染另一条链路。
- 方案：主 agent 运行链路只认 `CORECODER_*`；评估 judge 单独认 `CORECODER_EVAL_* / DASHSCOPE_*`，彻底切断 `OPENAI_*` 回退。
- 取舍：这样会牺牲一部分“开箱兼容 OpenAI 环境变量”的便利性，但换来的是运行链路可预测、可诊断，不再被评估模型配置污染。
- 验证：在显式设置错误的 `OPENAI_API_KEY` 污染环境下，`Config.from_env()` 仍稳定解析到 `MiniMax-M2.7 + CORECODER_API_KEY + CORECODER_BASE_URL`；全量测试通过 `95 passed`。
- 面试表达：我处理过一个典型的多模型配置串线问题，最终不是继续堆优先级判断，而是把运行模型和评估模型的环境变量命名空间彻底隔离。
- 更新时间：2026-06-03

### C011：评估题定义过窄会把“正确但不同实现”误判成失败
- 场景：全量 benchmark 里，“缺失模块创建”和“错误恢复”两道题分别出现了 agent 实际完成任务、但评分仍失败的情况。
- 问题：`create_missing_module` 只接受 `utils.py` 一种文件形态，忽略了 `utils/__init__.py` 这种同样正确的 Python 包实现；`ambiguous_edit_recovery` 又要求出现 recovery 轨迹，但 fixture 本身没有稳定制造第一次失败。
- 根因：题库定义把“实现路径”误当成“任务目标”，并且 recovery 题缺少对失败前提的强约束，导致 grader 在验证细节时偏离真实能力。
- 方案：给 outcome 增加最小的“任一满足”能力，允许多个等价文件路径；同时重写 recovery 题 prompt/fixture，显式制造共享字符串歧义，确保第一次精确替换会失败。
- 取舍：题库会稍微多一点 schema 复杂度，但比继续把实现细节硬编码进单一路径更合理；这里只做最小扩展，不引入通用 DSL。
- 验证：新增 `must_contain_any / must_change_any_files` 测试，并重跑相关任务以确认两类假失败被消除。
- 面试表达：我在做 agent benchmark 时专门处理了“题目定义过窄”的问题，把评估从考察单一路径改成考察目标是否达成，同时保证 recovery 题真的能稳定触发失败再恢复。
- 更新时间：2026-06-03

### C012：给本地 Coding Agent 增加手机控制而不把内核做胖
- 场景：希望通过 Telegram 在手机上远程控制 CoreCoder，但项目的核心价值是小而美的本地 coding agent runtime。
- 问题：如果把 Telegram、聊天命令、审批入口和任务恢复逻辑直接塞进 `agent.py` / `cli.py`，会快速破坏原有 runtime 边界，让教学型内核膨胀成耦合严重的单体程序。
- 根因：远程控制本质上是 channel adapter 层需求，而不是 agent loop 本身的职责；同时审批、checkpoint、trace 已经在内核里存在，重复实现一套远程状态系统只会制造分叉。
- 方案：参考 `claw0` 的 channel adapter 思路，新增独立 `remote/` 目录，把 Telegram Bot、消息格式化和 chat -> agent 管理层拆出去；远程层只复用 `TaskState + checkpoint + trace + approval`，不重做第二套状态机。
- 取舍：第一版只做单通道 Telegram，不做 FastAPI、多通道路由和 delivery queue，以控制代码量；代价是跨重启的聊天映射保持轻量，需要通过 `/resume <task_id>` 恢复任务。
- 验证：新增远程层单元测试，覆盖审批流、checkpoint 恢复、消息分片与聊天重置；同时保持未安装 Telegram 依赖时主 CLI 不受影响。
- 面试表达：我把“手机控制 agent”拆成了适配层问题，而不是继续堆进主循环，这样既能增加远程能力，又保住了核心 runtime 的清晰边界。
- 更新时间：2026-06-04

### C013：让 `/approve-all` 提升体验但不绕过真正危险的命令
- 场景：Telegram 和终端模式下，普通编码任务会连续触发多条 `bash` 审批，手动 `/approve` 十几次体验很差，因此需要增加“本任务后续普通确认自动放行”能力。
- 问题：如果简单把 `/approve-all` 实现成“所有 confirm 一律自动通过”，像 `rm -rf`、`Remove-Item -Recurse`、`git reset --hard` 这类高风险命令也会被顺带放行，安全边界会直接失效。
- 根因：原先策略层只有 `allow / confirm / deny` 三种动作，没有区分“普通确认”和“必须再次人工确认”的风险等级；同时 `BashTool` 和 `Policy` 对危险命令存在重复拦截，语义不一致。
- 方案：给 `PolicyDecision` 增加 `requires_manual` 元数据，把高风险删除/重置命令下沉到策略层做“强制人工确认”，普通确认才允许被 `TaskState.auto_approve_for_task` 自动放行；终端和 Telegram 共享同一套任务态开关。
- 取舍：状态机会多一个任务级字段，策略对象也会多一点元数据，但换来的是“体验优化”和“安全边界”同时成立；当前仍保留 fork bomb、`curl | bash` 这类命令的硬拒绝。
- 验证：新增测试覆盖 `/approve-all`、高风险命令二次确认、审批提示显示具体 `bash` 命令，并通过全量测试验证终端和 Telegram 两端行为一致。
- 面试表达：我没有把批量批准做成简单的布尔开关，而是把确认分成普通确认和强制人工确认两层，这样既解决了 agent 连续审批的体验问题，又没有把危险删除命令悄悄放过去。
- 更新时间：2026-06-04
