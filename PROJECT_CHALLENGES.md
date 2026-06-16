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
- 场景：项目根目录新增 `.env` 后，使用中转站模型运行 `autocode`。
- 问题：直接 HTTP 与 OpenAI SDK 最小样例均可成功调用，但 `autocode` 主程序持续返回 `401 Invalid API key`。
- 根因：`Config.from_env()` 中 `load_dotenv(..., override=False)` 不会覆盖已有系统环境变量，而当前终端已存在旧的 `OPENAI_API_KEY`，导致程序优先读取了错误密钥。
- 方案：改用 `AUTOCODE_API_KEY` 与 `AUTOCODE_BASE_URL` 作为项目级独立配置，避开全局 `OPENAI_*` 变量污染。
- 取舍：继续复用 `OPENAI_*` 兼容性更强，但容易受用户全局环境影响；改用 `AUTOCODE_*` 可显著提升项目隔离性与可诊断性。
- 验证：更新 `.env` 后，`python -m autocode -p "Reply with exactly: OK"` 成功返回模型结果。
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
- 方案：主 agent 运行链路只认 `AUTOCODE_*`；评估 judge 单独认 `AUTOCODE_EVAL_* / DASHSCOPE_*`，彻底切断 `OPENAI_*` 回退。
- 取舍：这样会牺牲一部分“开箱兼容 OpenAI 环境变量”的便利性，但换来的是运行链路可预测、可诊断，不再被评估模型配置污染。
- 验证：在显式设置错误的 `OPENAI_API_KEY` 污染环境下，`Config.from_env()` 仍稳定解析到 `MiniMax-M2.7 + AUTOCODE_API_KEY + AUTOCODE_BASE_URL`；全量测试通过 `95 passed`。
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

### C014：`/approve_all` 不能一刀切，要和工作区边界对齐
- 场景：终端和远程端都支持 `/approve_all`，但原策略把 `rm -rf`、`del`、`Remove-Item -Recurse`、`git reset --hard` 一律标成强制人工确认，导致用户即使只想清理工作区内日志，也无法复用自动批准。
- 问题：如果始终强制人工确认，`/approve_all` 会在常见本地开发操作上失去价值；但如果简单放开，又可能把工作区外的删除一起放行。
- 根因：危险命令的策略只按“命令类型”分类，没有把“目标路径是否仍在工作区内”纳入判断。
- 方案：把危险 shell 命令改成路径敏感策略：目标路径在工作区内时走普通 `confirm`，可被 `/approve_all` 自动放行；目标路径在工作区外时直接 `deny`。无显式路径的 `git reset --hard` 视为工作区内变更。
- 取舍：没有上完整 shell 语义解析器，只覆盖最高价值的删除/重置场景；这样代码量小，但仍然保留了“未覆盖命令形态”的风险。
- 验证：新增测试覆盖 `rm -rf build`、`rm -rf ../outside`、`del receive.log 2>nul` 以及 `/approve_all` 在同一任务内自动通过工作区内危险命令的行为；全量测试通过。
- 面试表达：我把 `/approve_all` 从“粗暴跳过审批”收敛成了“受工作区边界约束的自动批准”，这样既保留了终端体验，也没有放开越权删除。
- 更新时间：2026-06-09

### C012：给本地 Coding Agent 增加手机控制而不把内核做胖
- 场景：希望通过 Telegram 在手机上远程控制 AutoCode，但项目的核心价值是小而美的本地 coding agent runtime。
- 问题：如果把 Telegram、聊天命令、审批入口和任务恢复逻辑直接塞进 `agent.py` / `cli.py`，会快速破坏原有 runtime 边界，让教学型内核膨胀成耦合严重的单体程序。
- 根因：远程控制本质上是 channel adapter 层需求，而不是 agent loop 本身的职责；同时审批、checkpoint、trace 已经在内核里存在，重复实现一套远程状态系统只会制造分叉。
- 方案：参考 `claw0` 的 channel adapter 思路，新增独立 `remote/` 目录，把 Telegram Bot、消息格式化和 chat -> agent 管理层拆出去；远程层只复用 `TaskState + checkpoint + trace + approval`，不重做第二套状态机。
- 取舍：第一版只做单通道 Telegram，不做 FastAPI、多通道路由和 delivery queue，以控制代码量；代价是跨重启的聊天映射保持轻量，需要通过 `/resume <task_id>` 恢复任务。
- 验证：新增远程层单元测试，覆盖审批流、checkpoint 恢复、消息分片与聊天重置；同时保持未安装 Telegram 依赖时主 CLI 不受影响。
- 面试表达：我把“手机控制 agent”拆成了适配层问题，而不是继续堆进主循环，这样既能增加远程能力，又保住了核心 runtime 的清晰边界。
- 更新时间：2026-06-04

### C013：让 `/approve_all` 提升体验但不绕过真正危险的命令
- 场景：Telegram 和终端模式下，普通编码任务会连续触发多条 `bash` 审批，手动 `/approve` 十几次体验很差，因此需要增加“本任务后续普通确认自动放行”能力。
- 问题：如果简单把 `/approve_all` 实现成“所有 confirm 一律自动通过”，像 `rm -rf`、`Remove-Item -Recurse`、`git reset --hard` 这类高风险命令也会被顺带放行，安全边界会直接失效。
- 根因：原先策略层只有 `allow / confirm / deny` 三种动作，没有区分“普通确认”和“必须再次人工确认”的风险等级；同时 `BashTool` 和 `Policy` 对危险命令存在重复拦截，语义不一致。
- 方案：给 `PolicyDecision` 增加 `requires_manual` 元数据，把高风险删除/重置命令下沉到策略层做“强制人工确认”，普通确认才允许被 `TaskState.auto_approve_for_task` 自动放行；终端和 Telegram 共享同一套任务态开关。
- 取舍：状态机会多一个任务级字段，策略对象也会多一点元数据，但换来的是“体验优化”和“安全边界”同时成立；当前仍保留 fork bomb、`curl | bash` 这类命令的硬拒绝。
- 验证：新增测试覆盖 `/approve_all`、高风险命令二次确认、审批提示显示具体 `bash` 命令，并通过全量测试验证终端和 Telegram 两端行为一致。
- 面试表达：我没有把批量批准做成简单的布尔开关，而是把确认分成普通确认和强制人工确认两层，这样既解决了 agent 连续审批的体验问题，又没有把危险删除命令悄悄放过去。
- 更新时间：2026-06-04

### C014：Windows 下子进程输出编码不稳定会随机打崩 agent
- 场景：在 Windows + Telegram 远程控制场景下，agent 通过 `bash` 工具执行命令后，偶发出现 `UnicodeDecodeError: 'gbk' codec can't decode byte ...`。
- 问题：命令本身已经执行完成，但 Python 在后台 reader thread 读取 stdout/stderr 时因为系统默认 `gbk` 解码失败而抛异常，导致 agent 拿不到命令结果。
- 根因：`subprocess.run(..., text=True)` 会按宿主机默认编码解码输出；而真实命令输出可能混用 UTF-8、GBK 或工具自带编码，Windows 下尤其容易和默认控制台编码不一致。
- 方案：直接把 `Sandbox` 的子进程输出固定成 `utf-8` 解码，并配 `errors="replace"`，让运行时只坚持一种编码策略。
- 取舍：如果某些外部命令真的输出 GBK，文本里可能出现替换字符，但系统不会再因为编码异常崩掉；相比做多层编码兼容，这种实现更短、更稳定，也更符合项目“保持内核简洁”的方向。
- 验证：补充单元测试校验 `subprocess.run` 显式携带 `encoding="utf-8"` 和 `errors="replace"`，并通过全量测试。
- 面试表达：我在处理 Windows agent 的命令输出编码问题时，最终没有做复杂兼容，而是统一收敛到 UTF-8 策略，用少量信息损失换运行时稳定和代码简洁。
- 更新时间：2026-06-04

### C015：飞书远程控制要完整交互，但不能把本地 agent 改成网关系统
- 场景：希望在飞书里像 Telegram 一样直接发消息控制 agent，并通过卡片按钮完成审批。
- 问题：如果直接走公网 webhook + HTTP 回调，会额外引入服务暴露、挑战响应、回调鉴权和部署复杂度；但如果完全照搬 Telegram 的同步轮询模式，飞书事件一旦处理超过 3 秒就会重试。
- 根因：飞书应用机器人是事件驱动模型，消息接收和卡片回调都要求快速确认；而 coding agent 的一次工具链执行可能明显超过即时回调窗口。
- 方案：采用官方长连接模式接收消息与卡片事件，把回调主线程只做快速确认，真正的 `RemoteManager.submit / resolve_approval` 放进后台线程池；审批结果继续通过消息 API 回发，卡片只承担交互入口。
- 取舍：相比 Telegram 适配器，飞书层多了一层后台调度和消息封装，但避免了引入 FastAPI/webhook 网关；同时继续复用现有任务状态机，没有把项目演化成多通道路由平台。
- 验证：补充飞书文本解析、卡片按钮 payload、会话 key 和消息分片测试；全量测试通过。
- 面试表达：我在给本地 coding agent 加飞书控制时，没有直接把系统做成大网关，而是用官方长连接模型承接事件，再把耗时 agent 工作异步化，既满足 IM 回调约束，也保持了内核结构简洁。
- 更新时间：2026-06-04

### C016：1M 上下文模型下，沿用 128k 时代的压缩参数会白白浪费窗口
- 场景：项目开始接入 MiniMax 这类 1M 上下文模型，但 `ContextManager` 仍沿用 128k 默认预算、固定 `keep_recent=8/4`、以及 `flat[:15000]` 的小摘要输入上限。
- 问题：即使模型本身有更大的窗口，运行时也会过早开始压缩，旧对话摘要吃到的信息过少，最近轨迹保留得也太短，导致大窗口优势根本没发挥出来。
- 根因：压缩策略最初是按 128k 级别窗口写死的，用的是固定常数而不是与 `max_context_tokens` 相关的动态参数。
- 方案：把默认上下文预算提升到 1M，并让 `summary_keep_recent`、`collapse_keep_recent`、`summary_input_chars` 都跟窗口大小联动，在保持策略简单的前提下延后压缩、扩大摘要输入、保留更多近期消息。
- 取舍：更大的上下文预算会增加单轮提示词成本，但比起在 1M 模型上继续用小窗口参数，这种开销是合理的；当前仍保留 120k 字符摘要输入上限，避免 summarize 自己变成成本黑洞。
- 验证：补充单元测试校验 1M 窗口下的动态参数，并通过全量测试。
- 面试表达：我处理过一个典型的“大窗口模型接入但运行时还活在小窗口时代”的问题，核心不是盲目关掉压缩，而是把压缩阈值、摘要输入量和近期轨迹保留数量一起按窗口大小重标定。
- 更新时间：2026-06-04

### C017：系统长大后，平铺模块会反过来掩盖真实架构边界
- 场景：AutoCode 已经从单纯的本地 CLI agent 演化出 `runtime / state / context / remote / eval` 等完整层次，但 `autocode/` 根目录除了 `tools/` 和 `remote/` 之外仍基本平铺。
- 问题：目录结构没有体现系统边界，阅读者很难一眼看出哪些是 agent 编排、哪些是任务状态与持久化、哪些是上下文工程、哪些是执行基础设施；随着远程控制和评测加入，导入关系也越来越分散。
- 根因：项目早期以“最少文件先跑通”为目标，后续功能增长主要靠在根目录继续加模块，架构已经升级，但物理目录还停留在 demo 阶段。
- 方案：把内部模块按职责重组为 `agent / context / infra / runtime / state / remote / tools` 包结构；同时同步调整内部导入、测试和 eval，使主入口、远程入口和评测入口都落在新边界上。
- 取舍：这次选择直接改内部导入而不是保留一层旧路径兼容壳，代价是仓库内部引用需要一次性更新；换来的是目录边界真实、代码更短，也更符合“不要靠兼容层掩盖重构”的原则。
- 验证：重组后执行导入检查、CLI 启动检查和全量测试，结果 `123 passed`。
- 面试表达：我做过一次典型的“系统已经不是 demo，但目录还像 demo”的重构，重点不是单纯挪文件，而是先按职责重画边界，再把导入链、远程入口和评测体系一起迁过去，确保结构和运行时模型一致。
- 更新时间：2026-06-05

### C018：上下文压缩不应破坏原始 transcript
- 场景：AutoCode 的 `ContextManager` 会直接改写 `self.messages`，而 `checkpoint.json` 又把同一份 `messages` 当作恢复历史保存，导致一旦 compact，原始 user/assistant/tool 记录也随之丢失。
- 问题：会话一旦足够长并发生压缩，系统虽然还能基于 summary 继续运行，但无法再完整回放原始对话，也缺少 transcript 这个后续做 fork、导出、检索和审计的基础层。
- 根因：运行态上下文和原始消息记录被合并成了同一份状态对象，压缩逻辑天然是 destructive 的；早期实现优先保证“能压缩、能恢复运行”，还没有把 transcript 和 runtime context 分层。
- 方案：新增 `transcript.jsonl` 作为 append-only 原始消息日志，所有 user/assistant/tool 消息都通过 `Agent` 的统一追加入口同时写入 transcript；压缩仍只作用于运行态 `self.messages`，并额外记录 compact 元信息。
- 取舍：当前 checkpoint 仍默认恢复压缩后的运行态，而不是自动从 raw transcript 重建上下文；这样实现最简洁，先解决“原始记录丢失”的根本问题，把 transcript-aware resume / retrieval 放到后续阶段。
- 验证：新增 transcript 测试，确认单任务多轮执行发生 compact 后，`agent.messages` 变短但 `transcript.jsonl` 仍保留完整原始 user/tool/assistant 链路；全量测试 `125 passed`。
- 面试表达：我处理过一个典型的 agent transcript 设计问题，核心不是先做复杂检索，而是先把“活跃上下文可压缩”和“原始记录不可变”这两层状态彻底拆开，这样后续 resume、fork、审计和导出才有基础。
- 更新时间：2026-06-06

### C019：把规则、全局记忆、项目记忆和任务状态拆成四层上下文
- 场景：项目已经有 `AGENTS.md/CLAUDE.md`、Todo、Task、Checkpoint 和 Context Compression，但长期经验、项目事实和当前任务状态仍混在同一条 prompt 组装链里。
- 问题：如果继续把“用户长期偏好”“当前仓库真实环境”“当前任务执行状态”都塞进同一种 memory，后续会出现内容重复、层级冲突和上下文预算争抢。
- 根因：原 `MemoryManager` 只会读项目内静态文件，没有区分 global memory、project memory 和 task system 三种不同生命周期的信息。
- 方案：把规则层保持在 `AGENTS.md/CLAUDE.md`，新增用户级 `~/.autocode/MEMORY.md` 和项目级 `.autocode/PROJECT_MEMORY.md` 两层记忆；Task 继续独立，项目记忆只在压缩前和任务完成时做最小自动刷新。
- 取舍：当前只自动维护项目级记忆，不直接无感知写入全局记忆；这样能先获得跨会话项目事实沉淀，又避免把短期噪声污染长期偏好层。
- 验证：新增单元测试覆盖 global/project memory 读取与项目记忆去重写入，并通过真实 agent 运行验证 `PROJECT_MEMORY.md` 会自动生成。
- 面试表达：我把 agent 的上下文拆成了规则、全局记忆、项目记忆和任务状态四层，不靠大而全知识库，而是用最小文件系统设计解决跨会话经验沉淀与当前任务执行解耦的问题。
- 更新时间：2026-06-07

### C020：全局规则层和项目记忆层如果都存在，会出现职责重叠
- 场景：项目已经支持 `AGENTS.md/CLAUDE.md` 规则层，并尝试再引入全局 `MEMORY.md` 和项目级 `PROJECT_MEMORY.md` 两层记忆。
- 问题：全局 `MEMORY.md` 如果继续承载“用户长期偏好”和“跨项目习惯”，会和用户级 `CLAUDE.md/AGENTS.md` 高度重叠，导致规则和记忆边界模糊，用户也不清楚该把全局信息写到哪里。
- 根因：Claude Code 的项目自动记忆目录和用户级 `CLAUDE.md` 本来就是两套不同机制，而当前项目已经有稳定的规则层，继续叠加全局 memory 只会制造重复入口。
- 方案：移除全局 `MEMORY.md`，只保留 `AGENTS.md/CLAUDE.md` 作为全局规则与偏好层，memory 只保留项目级 `PROJECT_MEMORY.md`，并继续自动维护项目事实与坑点。
- 取舍：这样会失去一层独立的“全局自动记忆”，但换来的是系统更简单、职责更清楚，也更符合当前项目“小而精”的风格；后续如果真需要全局自动记忆，再单独设计，不与规则层混用。
- 验证：删除全局 memory 注入后重跑全量测试，并用真实模型再次验证项目运行结束后 `PROJECT_MEMORY.md` 仍会自动生成。
- 面试表达：我做过一次 agent 记忆层收敛，核心不是继续加层，而是主动删掉和规则层重叠的全局 memory，只保留项目级自动记忆，让系统入口更少、心智模型更稳定。
- 更新时间：2026-06-07

### C021：项目记忆如果沉淀“读代码就知道”的事实，会快速退化成重复噪声
- 场景：`PROJECT_MEMORY.md` 自动沉淀启用后，demo 工作区连续运行几次，项目记忆开始反复写入项目根路径、入口命令、函数签名等内容。
- 问题：这些事实重新读一遍源码就能得到，既挤占 prompt 预算，也会让真正高价值的坑点和非显式约束被淹没；同时不同措辞的重复 bullet 会持续累积。
- 根因：初版项目记忆提取提示词过宽，只做了“按整行精确去重”，没有区分“高价值长期记忆”和“显而易见代码事实”，也没有做近似重复归并。
- 方案：收紧项目记忆抽取标准，只保留非显式、可复用、容易重复踩坑的信息；本地再对低信号 bullet 做过滤，并基于归一化 key 合并近似重复内容。
- 取舍：过滤规则会牺牲一部分“万一以后有用”的宽松性，但能显著提高项目记忆密度；当前仍坚持轻量规则化去重，不引入 embedding/向量检索。
- 验证：补充测试覆盖低价值 bullet 过滤与近似重复归并，并清理 demo 工作区里已污染的 `PROJECT_MEMORY.md`。
- 面试表达：我处理过 agent 自动记忆“越记越吵”的问题，关键不是把记忆系统做得更重，而是先限定记什么、再做本地信号过滤和语义级去重，让长期记忆只保留真正值得占上下文的事实。
- 更新时间：2026-06-08

### C022：Windows 本地编码会把 agent 的文件读写链路悄悄搞坏
- 场景：在中文注释较多的教学项目上跑真实 agent 任务，模型先读 `README.md / app.py / session_store.py`，随后尝试用 `write_file` 重写文件。
- 问题：`read_file` 读出的中文出现整片乱码，随后 `write_file` 在写回时触发 `gbk` 编码异常，甚至把目标文件先截断成空文件，导致任务和工作区双双受损。
- 根因：`WorkspaceFS.read_text()/write_text()` 与 `write_file` fallback 没有显式指定 UTF-8，Windows 下会退回本地默认编码；而 agent 的消息流又把乱码内容继续喂回给模型，放大了破坏性。
- 方案：把文件系统层和工具 fallback 的文本读写统一收敛为 UTF-8；读取时显式 `errors=\"replace\"`，写入时统一 `encoding=\"utf-8\"`。
- 取舍：这会要求工作区文本文件默认按 UTF-8 处理，但相比继续依赖平台默认编码，更稳定、更可预测，也与项目整体中文/跨平台使用场景一致。
- 验证：补充 `WorkspaceFS` UTF-8 读写测试，并在真实 `redis_work` 任务复盘中确认根因来自编码链路而非业务代码。
- 面试表达：我处理过一个典型的 agent 运行时问题，不是模型推理错了，而是底层文件读写用了系统默认编码，导致“读取乱码 -> 模型误改 -> 写回失败/截断”形成破坏链，最终我把整条文件工具链统一收敛到 UTF-8 才稳定下来。
- 更新时间：2026-06-08

### C023：飞书单聊天页里做会话恢复，不能把内部 task id 直接暴露给用户
- 场景：飞书远程控制只有一个聊天页，用户需要恢复当前项目的历史会话，但不可能记住 `task_id`，也没有 CLI 那样天然的列表选择界面。
- 问题：如果继续要求 `/resume <task_id>`，用户必须先理解 checkpoint 与 task id，恢复入口会从“会话恢复”退化成“底层状态操作”；同时不同工作区的历史会话还可能混在一起。
- 根因：远程层之前只有 chat session key，没有“项目内会话列表”这一层产品抽象；checkpoint 里也没有持久化 workspace 元数据，无法按当前项目过滤。
- 方案：在 checkpoint 中写入标准化后的 `workspace_root`，`/resume` 直接读取当前项目最近会话，并通过飞书卡片按钮恢复，不再让用户手输 task id。
- 取舍：这次只做“最近 10 条 + 按钮恢复”，不加编号恢复、分页或多项目切换，先把主流程做顺，避免远程命令设计回退成底层实现细节。
- 验证：补充 checkpoint/workspace 过滤测试、远程管理器过滤测试和飞书恢复卡片测试，确认 `/resume` 只返回当前工作区的会话并可直接点击恢复。
- 面试表达：我处理过一次 IM 端 agent 会话恢复设计，关键不是把 CLI 命令原样搬到聊天页，而是先给 checkpoint 加项目归属，再把恢复入口从技术性的 task id 改成项目内会话卡片选择。
- 更新时间：2026-06-08

### C024：工具仓库自带 `.env` 会越权覆盖真实工作区
- 场景：在 `G:/mycode/redis_work` 目录启动 `autocode-feishu`，预期当前项目就是工作区，但 agent 实际回到了工具仓库目录。
- 问题：远程 agent 明明是在业务项目里启动，读写和会话却落到另一个仓库，工作区语义完全失真。
- 根因：配置加载逻辑除了向上查找当前工作区的 `.env`，还会额外回退读取工具仓库根 `.env`；一旦其中存在 `AUTOCODE_WORKSPACE_ROOT`，就会覆盖 `Path.cwd()`。
- 方案：删除仓库级 `.env` 回退，只保留“显式环境变量 + 当前工作区向上查找 `.env`”两种来源，让 `cwd` 稳定成为默认工作区。
- 取舍：这样会失去把公共默认配置偷偷放在工具仓库 `.env` 里复用的便利，但换来的是多项目场景下行为可预期，远程控制不会跑错仓库。
- 验证：补充测试覆盖工作区 `.env` 加载和 `workspace_root == Path.cwd()` 默认行为，确认在目标项目目录启动时不再跳回工具仓库。
- 面试表达：我处理过一个很隐蔽的 agent 配置优先级问题，表面看是工作区解析错了，实际根因是工具仓库自带 `.env` 越权覆盖了业务项目，最后通过收紧配置来源边界把“在哪启动就服务哪个项目”做成了稳定规则。
- 更新时间：2026-06-08

### C025：CLI 和 IM 端的“会话”抽象不统一，最终会把用户逼回底层 checkpoint 概念
- 场景：CLI 端同时保留了“只存消息历史的 session”和“带任务状态的 checkpoint”，飞书端则已经只支持基于 checkpoint 的 `/resume`。
- 问题：同一个项目里，CLI 和飞书看到的“会话”不是同一批数据；用户在 CLI 里恢复的是聊天历史，在飞书里恢复的是任务现场，命令名相似但语义不同，长期会把产品心智撕裂。
- 根因：早期 CLI 为了快速落地做了轻量 `session.py`，而远程控制后来又需要 `workspace_root / pending_approval / todo / step` 这类任务态信息，只能绕回 checkpoint，导致产品层和存储层分叉。
- 方案：删除独立 `session.py`，CLI 也统一到 checkpoint 级 `/resume`；只保留一套“当前项目可恢复会话”数据源，让 CLI 和飞书共享同一批会话。
- 取舍：这样会失去“只恢复纯聊天记录”的轻量入口，但换来的是跨端一致性，尤其是审批中断、todo、步骤号和项目过滤都能在所有入口保持一致。
- 验证：删除 `/sessions`、`/tasks`、`--resume-task` 与 `session.py`，补充 CLI 当前项目过滤测试，并全量跑通测试。
- 面试表达：我处理过 agent 产品里一个很典型的抽象分裂问题，表面是 CLI 和 IM 命令不统一，实质是“聊天历史”和“任务现场”用了两套存储语义，最后我把产品层统一成 session，把底层统一到 checkpoint 级状态，才把跨端体验收拢。
- 更新时间：2026-06-09

### C026：CLI 审批如果混用两套输入栈，会把交互和任务状态一起搞乱
- 场景：CLI 主循环使用 `prompt_toolkit`，但工具审批仍通过裸 `input()` 同步阻塞；一旦用户批准后工具执行时间较长，再按 `Ctrl+C` 中断，就会出现终端像卡死、后续会话报 tool-call 配对错误。
- 问题：审批阶段和正常聊天阶段不是同一套交互模型，用户不知道当前是在等输入还是在跑工具；更严重的是，assistant tool_call 已写入历史、pending 已清掉，但 tool result 还没写回时被中断，会留下非法消息历史。
- 根因：CLI 交互层混用了 `prompt_toolkit` 和 `input()`；任务状态层又把“清 pending”和“写 tool result”分成了两个不可中断的阶段，中间缺少补位逻辑。
- 方案：审批统一改成 `prompt_toolkit` 弹窗选择，主聊天路径不再传同步 `approval_handler`；如果工具执行期被 `KeyboardInterrupt` 打断，就自动回填一条占位 tool result，让消息历史继续保持成对。
- 取舍：这样会把审批体验做成更强约束的模态选择，不再是完全自由输入；但换来的是 CLI/飞书语义统一、普通消息不会误入审批阶段、中断后也不会把会话状态打坏。
- 验证：补充 CLI 审批弹窗测试和工具执行中断补位测试，并全量跑通测试，确认 pending 审批和中断恢复都正常。
- 面试表达：我处理过一个 agent CLI 的典型一致性问题，表面是审批体验差，实质是输入栈分裂和状态提交顺序不安全，最后通过统一交互模型并在中断时补齐 tool result，把“卡住”和“历史损坏”两个问题一起收掉。
- 更新时间：2026-06-09

### C027：长运行命令不能继续塞进同步 bash，必须提升成受控后台进程
- 场景：RabbitMQ `receive.py`、Web 开发服务器、worker 这类命令天然会持续运行；如果 agent 直接用同步 `bash` 执行，CLI 会一直等到进程退出，看起来就像卡死。
- 问题：agent 无法在消费者/服务进程运行期间继续执行后续步骤，也拿不到结构化的进程状态、日志尾部或停止入口，导致本该可自动化的联调任务只能半途卡住。
- 根因：当前 `bash` 底层固定使用 `subprocess.run(...)`，其设计目标是“命令结束后一次性返回结果”，与长期运行进程的生命周期完全不匹配。
- 方案：新增 `start_process / read_process_output / wait_for_process_output / stop_process` 四个最小工具，把长运行命令从同步调用链剥离成“启动后返回句柄，再按需观察和收尾”的后台任务模型。
- 取舍：这会引入一个小型进程管理层，并要求 agent 多走几步（启动、等待日志、停止），但换来的是 RabbitMQ、dev server、worker 这类真实工程场景终于能被稳定处理。
- 验证：补充后台进程工具的 round-trip 测试和 `start_process` 审批测试，并全量跑通测试。
- 面试表达：我处理过 agent 在真实工程场景里“不是不会写代码，而是不会托管长运行进程”的问题，关键不是继续给 bash 打补丁，而是把这类命令提升成受控后台任务，前台拿句柄、后台跑进程、再通过日志和停止接口完成联调闭环。
- 更新时间：2026-06-09

### C028：Windows 下 `shell=True` 启动后台命令时，只杀父进程会留下“幽灵 worker”
- 场景：agent 用 `start_process("python worker.py")` 启动 RabbitMQ worker，任务结束后调用停止逻辑，本以为进程已清理，但系统里仍残留 `python worker.py` 持续消费消息。
- 问题：用户看到工具已经返回“Stopped background process”，实际消费者还活着，后续再发消息会被旧 worker 抢走，导致联调结果混乱且难以排查。
- 根因：Windows 下 `shell=True` 会形成 `cmd.exe /c ... -> python.exe worker.py` 的父子树；原实现只对 `Popen` 对应的父进程做 `terminate()/kill()`，没有递归终止子进程。
- 方案：`stop_process()` 在 Windows 分支改为调用 `taskkill /PID <pid> /T /F` 杀整棵进程树；同时在 system prompt 明确要求 agent 测试完后台进程后主动调用 `stop_process`。
- 取舍：引入平台分支会让进程管理代码略复杂，但比继续假设 `proc.kill()` 能覆盖子进程更可靠；Windows 用系统级 `taskkill`，POSIX 仍保留 `killpg` 语义。
- 验证：用 `G:/mycode/Rabbitmq_work/worker.py` 真实复现，修复前杀掉父 `cmd.exe` 后 `python worker.py` 仍存活；修复后 `stop_process` 执行完成后同一 PID 树已不存在，并补充 Windows 分支单测。
- 面试表达：我处理过一个很典型的跨平台进程托管坑，表面是“停止命令失效”，实质是把 shell 包装进程当成了业务进程本体；最后通过按平台正确管理进程树，把 RabbitMQ worker 这类长运行任务从“假停止”修成了真正可回收。
- 更新时间：2026-06-09

### C029：多工具审批如果只记住第一个 pending，会把 tool call / result 配对打坏
- 场景：模型一轮同时返回多条需要确认的 `bash` 调用，用户批准第一个后 agent 继续执行，但 provider 随后返回 `tool call and result not match`。
- 问题：同一条 assistant 消息里有多条 tool call，但历史里只补回了第一条 tool result；下一轮请求带着不完整的工具配对历史发给模型，直接触发 400。
- 根因：任务状态里只有单个 `PendingApproval`，批准恢复时只重放当前这一条工具调用，然后立刻进入下一轮 LLM；同批剩余 tool call 没有被持久化，也没有在进入下一轮前补齐。
- 方案：保持单个 `pending_approval` 入口不变，但给它补一个 `remaining_tool_calls` 队列；用户批准当前工具后，先继续处理同批剩余 tool call，只有当这一批全部拿到 result 后才允许再次请求 LLM。
- 取舍：没有把审批状态升级成完整批处理状态机，仍维持现有 CLI/远程接口；代价是 `PendingApproval` 多带一个轻量队列字段，但复杂度远小于重做消息调度层。
- 验证：新增回归测试覆盖 `approve_all` 与“同批下一个工具继续等待审批”两条路径，并全量跑通 `137 passed`，确认不会再出现只回写首个 tool result 的历史损坏。
- 面试表达：我处理过 agent 工具调用协议的一致性 bug，表面看是某个模型接口挑剔，实质是我们自己在多工具审批恢复时破坏了 assistant/tool 的配对约束；最后通过把“当前待审批 + 同批剩余队列”作为最小状态补全，修掉了协议错误又没把系统做重。
- 更新时间：2026-06-09

### C030：如果不在运行时记录，历史任务就无法精确回放真实 LLM 输入输出
- 场景：任务目录已经有 `checkpoint.json`、`audit.jsonl`、`transcript.jsonl`，但用户希望像 `real_llm_rounds.md` 一样逐轮查看真实送入模型的 `messages/tools` 和模型原始返回。
- 问题：事后只能重建 user/tool/assistant 消息链，无法保证每轮 system prompt、tools schema、token 用量与真实请求完全一致。
- 根因：原系统只在 LLM 调用前后记录轻量事件，没有把“模型请求体 + 模型响应体”本身持久化。
- 方案：新增 `LLMRoundRecorder`，在每次真实 `llm.chat(...)` 返回后立即把该轮 `messages/tools/response` 落到任务目录下的 `llm_rounds.jsonl` 和 `llm_rounds.md`。
- 取舍：任务目录体积会增加，但调试、复盘、对账和面试展示都能直接基于真实轮次，不再依赖事后猜测。
- 验证：新增多轮任务测试，确认每个任务都会自动生成 `llm_rounds.md/jsonl`，且 checkpoint/task 元数据同步发布对应文件名。
- 面试表达：我把 agent 的 LLM 调用从“只有摘要指标”升级成“逐轮可回放资产”，关键不是多打一份日志，而是把真实请求和真实响应在运行时就固化下来，避免事后重建失真。
- 更新时间：2026-06-10

### C031：Windows 下 stdout 被重定向时，子 Python 进程不会自动跟随父进程的 UTF-8 解码策略
- 场景：`bash` 和 `start_process` 已经把父进程读取侧固定成 UTF-8，但运行 `python new_task.py`、`python worker.py` 后，中文日志仍然同时在前台输出和后台日志里变成乱码。
- 问题：表面看像“读错编码”，实际连后台日志文件里落下的字节都已经坏了，说明问题发生在子进程输出阶段。
- 根因：Windows 下当 stdout 被重定向到管道或文件时，子 Python 进程仍可能按本地代码页输出；父进程 `Popen(..., encoding=\"utf-8\")` 只影响读取端，无法反向约束子进程编码。
- 方案：在统一沙箱环境里强制注入 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`，让 `bash` 与 `start_process` 启动出来的 Python 子进程都按 UTF-8 输出文本。
- 取舍：如果极少数脚本显式依赖本地代码页，行为会被收敛到 UTF-8；但对当前项目“所有持久化文本统一 UTF-8”的目标更一致，也比做多套编码猜测更短。
- 验证：补充环境构建测试，确认运行时必带 `PYTHONIOENCODING/PYTHONUTF8`；结合 RabbitMQ worker 场景，可解释此前前后台中文同时乱码的根因。
- 面试表达：我处理过一个很容易误判的 Windows 编码问题，关键不是继续猜“该用 GBK 还是 UTF-8 读”，而是认识到子进程在 stdout 重定向场景下会独立决定输出编码，必须从环境层统一收口。
- 更新时间：2026-06-10

### C032：同一 CLI/聊天会话里自动切换 task_id，会把会话语义和持久化语义撕裂
- 场景：CLI 和远程层都把一个连续对话称为 session/chat，但 `Agent` 在任务一旦 `completed/failed` 后，下一条用户消息会自动新建 `task_id`。
- 问题：同一个会话里会生成多个 `C:/Users/.../.autocode/tasks/task_xxx` 目录；用户看到还是同一聊天窗口，但 `/resume`、checkpoint、trace、transcript 却被切成多段。
- 根因：`Agent._ensure_task()` 以“当前 task 是否完成”作为换 id 条件，而不是以“当前会话是否被 reset / replace”作为换 id 条件；同时 `self.messages` 又不会在换 task 时清空，导致上下文连续、持久化却断裂。
- 方案：把 `task_id` 语义收敛成“当前 agent 会话的持久化 id”：只有 `task_state is None` 时才新建，`/reset` 或显式 resume/replace 时再切换。
- 取舍：这样 `task` 更接近 session 而不再是“单次完成事项”，标题默认保持首次用户输入；但比起同会话里无声切目录，这个语义更稳定，也和 CLI/remote 的 `/resume session` 文案一致。
- 验证：补充回归测试，确认同一个 `Agent` 连续两次 `chat()` 复用同一 `task_id`，同一个 `RemoteManager chat_id` 连续 `submit()` 也复用同一 `task_id`。
- 面试表达：我处理过 agent 产品里一个典型的抽象错位问题，UI 层说的是 session，持久化层却悄悄按完成态切 task_id；我最后把 id 生命周期改成“会话级”，让用户看到的会话边界和底层 checkpoint 边界重新对齐。
- 更新时间：2026-06-10

### C033：Session 和 Task 共用一个状态模型，会让目录结构、恢复语义和日志边界长期纠缠
- 场景：系统已经开始同时支持 CLI、飞书、Telegram、checkpoint 恢复、trace、transcript 和 llm_rounds，但底层仍把 `TaskState.task_id` 同时当作“聊天会话 id”和“当前任务 id”使用。
- 问题：一旦用户要求“同一聊天窗口下可以有多个任务”，整个系统都会出现命名和语义错位：目录叫 `tasks`，`/resume` 实际恢复的是聊天现场，trace/transcript 也无法明确到底按会话聚合还是按任务聚合。
- 根因：状态层缺少真正的两层模型，只有一个 `TaskState` 承担了会话持久化、当前任务状态、恢复入口和日志主键四类职责。
- 方案：新增 `SessionState(session_id, current_task)` 作为持久化根，所有 checkpoint/trace/transcript/llm_rounds/audit 统一落到 `sessions/<session_id>/`；`TaskState` 只表示当前任务，保留独立 `task_id`，在同一 session 内可轮换。
- 取舍：这样会带来一轮较大的链路迁移，需要同步修改 Agent、Runtime、CLI、远程端和测试；但换来的是抽象边界真正稳定，用户看到的 session 概念终于和底层目录、恢复和日志边界一致。
- 验证：完成后全量回归 `pytest -q tests eval` 通过 `143 passed`；同一 chat/session 会复用 `session_id`，而任务完成后下一次用户输入会创建新的当前 `task_id`。
- 面试表达：我做过一次 agent 状态模型真正的拆层，不是简单改名，而是把“会话持久化边界”和“当前任务执行边界”分开，让 checkpoint、trace、transcript、远程恢复和 UI 文案全部围绕同一套 session/task 语义收口。
- 更新时间：2026-06-10

### C034：把跨会话提示直接塞进 prompt，很容易演变成低信号噪声层
- 场景：系统一边有 `PROJECT_MEMORY.md` 作为长期记忆，一边又在每轮 prompt 里追加 `Recent Sessions`，同时项目记忆还会由模型自动重写。
- 问题：模型每轮都会看到“最近 3 个 session 的状态摘要”，但这些摘要只有 `session_id/status/step/model`，几乎不提供真正可复用的项目知识，反而和长期记忆争上下文预算。
- 根因：把“当前会话压缩”和“跨会话连续性”混成了一个问题处理；前者应由 context compression 解决，后者应由高信号 `PROJECT_MEMORY.md` 承担，而不是再叠一层弱摘要。
- 方案：删除 `Recent Sessions` 注入，只保留 `PROJECT_MEMORY.md` 作为跨会话连续性来源；同时给项目记忆增加本地过滤，主动丢弃 workspace 路径、task/session/proc id、日志路径、固定操作顺序、一次性验证结果等低信号内容。
- 取舍：这样会失去一层“最近跑过什么”的即时提示，但 prompt 更干净，长期记忆边界也更明确；真正有价值的跨会话事实应该沉淀为 durable memory，而不是最近历史列表。
- 验证：补充测试覆盖 `Recent Sessions` 不再注入，以及低信号 runbook 行会被过滤；相关回归通过。
- 面试表达：我处理过 agent memory 层一个常见退化问题，不是记得太少，而是把低信号历史也当成记忆塞进 prompt；最终我把跨会话连续性收敛到高信号项目记忆，并用本地规则过滤掉 runbook 和临时状态噪声。
- 更新时间：2026-06-10

### C035：记忆更新如果同步挂在主任务结束路径上，会直接拖慢用户看到最终回复的时间
- 场景：`PROJECT_MEMORY.md` 需要在任务完成后基于整段对话重写，但这一步本质上又是一次额外的 LLM 调用。
- 问题：如果在主循环里同步执行，用户虽然已经“完成任务”，却还要额外等待 memory 总结这次后台调用结束，体验上像任务收尾突然变慢。
- 根因：记忆系统和主 session 共用同一条同步执行链路，任务结果返回与长期记忆更新被绑成了一个事务。
- 方案：把 memory 刷新改成后台静默任务，主 session 只负责提交 snapshot；后台使用独立的 LLM 副本异步重写 `PROJECT_MEMORY.md`，不阻塞当前回复。
- 取舍：异步后用户体验更顺，但 memory 写入不再与主回复严格同步完成；这里接受“最终一致性”，因为长期记忆不是当前轮 correctness 的必要条件。
- 验证：补充测试覆盖 agent 在任务完成时只调度后台 refresh，不阻塞主返回；全量回归通过。
- 面试表达：我把 agent 的长期记忆更新从同步尾处理改成了后台静默任务，核心取舍是把用户当前轮的交互延迟和跨会话记忆的一致性解耦，前台先快返回，后台再做最终沉淀。
- 更新时间：2026-06-10

### C036：小型 coding agent 的审批策略如果走“默认 confirm”，交互成本会迅速压过安全收益
- 场景：本地 coding agent 在 Windows 联调项目时，大量 `curl`、`netstat`、`wmic`、启动后台进程之类的常规操作都被 runtime 打成审批请求。
- 问题：用户只做一次后端联调，就出现了数十次审批；同时模型还能在工具被拒绝后把 todo 直接标成 completed，造成“很谨慎但又不一致”。
- 根因：审批层采用了“少量白名单 + 默认 confirm”的中间策略，导致未知命令大多需要人工点选；而 todo 状态又完全相信模型自报，没有利用最近一次工具结果做约束。
- 方案：把策略收敛成“硬边界 deny，其他 allow”：保留工作区越界、受保护文件、危险删除、`curl | bash`、`git reset --hard` 这类硬拒绝；取消默认 confirm。同时给 `todo_write` 增加最小守卫，最近一次工具结果如果是 `Blocked by policy` 或 `Error:`，就拒绝把已有 todo 从未完成改成 completed。
- 取舍：这样会牺牲一部分“人工兜底确认”能力，但显著降低交互摩擦，更符合小而精 agent 的定位；一致性守卫只做最近一次结果检查，没有升级成重型任务证据系统。
- 验证：补充 policy、runtime、process、checkpoint、tools 相关回归测试，修改后定向测试全部通过。
- 面试表达：我在一个本地 coding agent 上做过审批模型瘦身，关键不是简单把权限放开，而是把策略改成“只在硬边界拒绝”，把安全收敛到真正危险的地方，再用极小的状态守卫补上 todo 语义一致性。
- 更新时间：2026-06-12

### C037：Windows 命令输出编码不统一时，越早强行按 UTF-8 解码，越容易把真实信息永久损坏
- 场景：agent 在 Windows 下通过 `bash` 和后台进程执行 `curl`、`wmic`、`findstr`、Python 脚本等命令，中文输出偶发乱码。
- 问题：即使运行时已经给 Python 子进程注入 `PYTHONIOENCODING/PYTHONUTF8`，非 Python 命令仍可能按本地代码页输出；如果 `subprocess` 一开始就按 UTF-8 解码，错误字节会直接变成替换字符，后续无法恢复。
- 根因：输出侧是多源编码，读取侧却在 `subprocess.run/Popen` 层提前做了单一 UTF-8 假设，原始 bytes 没有保留下来。
- 方案：把前台 shell 和后台日志统一改成“先保留 bytes，再走共享 `decode_output()` 解码函数”；按 `utf-8 -> 系统首选编码 -> gb18030 -> utf-8 replace` 的顺序解码，避免为不同命令写分支兼容。
- 取舍：相比直接强推某一种 shell 或 code page，这种方案代码更短、侵入更小；代价是极少数混合编码输出仍可能退化到 replace，但不会再因为过早解码而不可逆损坏更多信息。
- 验证：补充 `sandbox` 和 `processes` 测试，覆盖 UTF-8 输出、GB18030 回退解码、后台日志 bytes 读取，定向测试通过。
- 面试表达：我处理过 Windows agent 的命令输出乱码问题，关键不是继续赌“到底该按 GBK 还是 UTF-8 读”，而是把解码时机后移，先保住原始 bytes，再用一套很小的回退策略统一解码。
- 更新时间：2026-06-13

### C038：长任务达到轮数上限时直接硬失败，会把“已完成的上下文”一并丢给用户自己重建
- 场景：coding agent 在多轮查文件、跑命令、改配置后达到 `max_rounds`，当前实现会立即把任务标成失败并返回固定错误串。
- 问题：用户看到的不是“做到哪了、卡在哪、下一步怎么接”，而只是一个抽象上限错误，导致明明已经做了不少工作，最后却像突然断电。
- 根因：主循环只有正常完成和硬失败两种出口，没有在上限触发时做一次禁止工具调用的收尾总结。
- 方案：保留 50 轮硬上限，但在触顶后额外发起一轮“无工具总结”调用，要求模型用固定结构输出“已完成 / 当前卡点 / 建议下一步”，并提示用户可回复“继续”再开下一段任务。
- 取舍：没有引入“到上限后继续自动跑”的新调度逻辑，避免 runtime 变成软上限死循环；代价是任务仍会被标成 failed，但用户至少能拿到可继续协作的总结。
- 验证：补充 runtime 回归测试，确认 `max_rounds=1` 时会返回总结文本而不是固定错误串，同时任务状态保持 failed 且保留 round-limit 错误。
- 面试表达：我优化过一个 coding agent 的长任务收尾体验，关键不是把上限放开，而是在硬上限前插入一次无工具总结，让任务从“突然断掉”变成“可交接、可续跑”的失败。
- 更新时间：2026-06-13

### C039：聊天附件和云盘文件不是一套资源键，IM 机器人要发 PDF 不能直接复用 Drive 上传链路
- 场景：飞书远程控制已经支持文本和卡片，但用户希望把当前项目生成的截图或 PDF 直接作为聊天附件发回会话。
- 问题：如果沿用云文档/Drive 上传思路，只能得到云盘资源标识，最终在聊天里更像分享链接，不是原生图片或文件消息。
- 根因：飞书 IM 的图片和文件消息依赖 `image_key/file_key`，必须先走 `im/v1/images` 或 `im/v1/files` 上传，再用对应 `msg_type=image/file` 发送；这和 Drive 的文件标识不是同一套协议。
- 方案：先在飞书适配层补 `/send_image <path>` 和 `/send_file <path>`，再继续收敛成只在飞书通道注入的 `feishu_send` 工具，让 agent 能在识别到“把截图/PDF发我”这类意图后自己调用。
- 取舍：没有把飞书发送能力塞进全局工具表，而是只在 FeishuBot 构造 agent 时额外注入，避免 CLI/Telegram 平白暴露无效工具；代价是 RemoteManager 需要顺手补一个极小的 tool clone 逻辑，防止多聊天共享同一个工具实例。
- 验证：补充图片/文件消息 content 构造、上传请求 URI、工作区路径校验、文件类型映射、飞书发送 tool 与 RemoteManager clone 行为测试，定向回归通过。
- 面试表达：我做过一次 IM 机器人附件回传能力补齐，关键不是简单多包一层“上传文件”接口，而是先厘清聊天附件协议和云盘协议不是一回事，再把发送能力做成只在飞书通道暴露的 agent tool，这样模型能自主调用，但核心 runtime 不会被渠道逻辑污染。
- 更新时间：2026-06-13

### C040：进程清理如果依赖 Prompt，自然会在 reset/exit/异常收尾时失效
- 场景：agent 支持 `start_process / stop_process` 启动本地服务、worker 和 watcher，早期实现只在 system prompt 里提醒模型“结束前记得 stop_process”。
- 问题：显式调用 `stop_process` 时可以正确杀整棵进程树，但用户直接 `exit`、`/reset`、remote chat reset、runtime replace 或任务异常结束时，后台进程仍可能残留。
- 根因：资源回收职责被放在模型行为层，而不是 runtime 生命周期层；Prompt 只能影响“模型会不会想起来停”，不能覆盖宿主退出和中断。
- 方案：删除资源清理提示词，把责任下沉到 runtime：`start_process` 记录 `task_id + keep_alive`，任务完成/失败时自动清理临时进程，CLI 退出、`/reset`、远程 reset 和 runtime replace 时统一 `cleanup_all()`。
- 取舍：这样会多一层轻量进程托管状态，但边界更清楚；当前把 `keep_alive` 只定义为“跨任务保留”，不承诺跨 agent 退出继续托管，避免留下无跟踪进程。
- 验证：补充真实 Windows 父子进程测试，确认 `stop_process` 通过 `taskkill /T /F` 会杀整棵树；再补单元测试覆盖 task cleanup、reset cleanup、remote reset/replace cleanup，定向测试通过。
- 面试表达：我把 agent 的资源清理从 Prompt 规则改成了 runtime 生命周期职责，核心思路是“模型只表达意图，宿主负责最终回收”，这样才能真正消除残留和孤儿进程问题。
- 更新时间：2026-06-13

### C041：如果同时保留 `stop_process` 和任意 shell 杀进程能力，runtime 边界就会被模型一条 bash 绕穿
- 场景：agent 已经具备受管后台进程体系，`stop_process(process_id)` 只会停止自己登记过的进程；但模型仍可通过 `bash` 生成 `Get-Process python | Stop-Process -Force` 之类命令。
- 问题：一旦 shell 里还能随意杀进程，模型就可能绕过受管进程边界，误杀宿主 Python、自启服务或其他无关进程，导致任务日志停在中途、状态卡在 running。
- 根因：进程管理边界虽然在工具层已经建立，但策略层没有把“shell 终止进程”和“受管 stop_process”分离，等于留了一条后门。
- 方案：在 `policy.py` 里直接硬拒绝所有 shell 进程终止命令（`taskkill`、`Stop-Process`、`pkill`、`killall`、`kill <pid>` 等），要求 runtime 里只保留 `stop_process` 这一个正式出口。
- 取舍：这样会失去 agent 通过 shell 停止“外部非受管进程”的能力，但换来的是进程边界绝对清楚，没有“工具层受管、bash 层失控”的双轨语义。
- 验证：补充 policy 测试，覆盖 `taskkill /PID ... /T /F` 和 `Get-Process python | Stop-Process -Force`，确认都直接返回 deny，并提示改用 `stop_process`。
- 面试表达：我做过一次很典型的 runtime 边界收敛，不是继续在 shell kill 命令里做白名单兼容，而是直接删掉这条能力，只保留受管的 `stop_process` 出口，让“谁能杀谁”从 prompt 约定变成策略强约束。
- 更新时间：2026-06-13

### C042：项目记忆只看最近聊天不看真实源码时，很容易把“常见最佳实践”误写成“项目事实”
- 场景：agent 在任务结束后会自动重写 `.autocode/PROJECT_MEMORY.md`，原实现主要依据最近若干条对话内容做总结。
- 问题：模型会把“SQLAlchemy 2.0 常见写法”“一般项目习惯”之类的泛化知识，错写成当前仓库已经存在的事实，导致 memory 半对半错。
- 根因：记忆生成缺少源码侧 grounding，只看聊天里的自然语言总结，没有把真实项目文件片段一起作为证据输入；同时去重键只基于对话，源码变化但对话不变时还会跳过刷新。
- 方案：改成两阶段 memory grounding：先把工作区候选文本文件清单交给 LLM，让它自己选择最值得读取的文件路径；再读取这些真实文件片段作为 `Project file evidence` 供最终记忆总结使用。prompt 明确要求“只允许写有证据支持的事实，禁止按通用最佳实践推断”；刷新 key 同时纳入项目文件清单变化。
- 取舍：会额外增加一次很小的 LLM 选文件调用，但避免把“哪些文件重要”硬编码在代码里；代码仍保持轻量，不引入重型索引或本地规则过滤器。
- 验证：补充测试覆盖 memory 先看到 `Project file inventory`、由 LLM 选择路径、最终 prompt 只注入选中的真实文件片段、忽略 `.venv` 等运行目录，并验证文件清单变化会触发新的 refresh key。
- 面试表达：我修过 agent memory 一个很典型的可信度问题，关键不是继续手写高价值文件名单，而是把“先选证据，再写记忆”做成两阶段链路，让模型自己决定该读哪些项目文件，但最终只能基于真实片段落笔。
- 更新时间：2026-06-13

### C043：Agent 评测如果只看“是否改对文件”，很容易被改样例数据或硬编码输出的投机路径骗过
- 场景：给本地 `eval/` 子系统新增“多文件 + 隐藏 bug”任务时，agent 很容易通过改 `main.py`、样例输入、路由表或 `.env` 直接把验证脚本跑绿，而不是真修根因。
- 问题：如果 grader 只校验最终输出或某个文件包含目标字符串，会把“修业务逻辑”和“篡改样例/绕过校验”混为一谈，导致 benchmark 虚高。
- 根因：复杂任务里真正的 bug 往往藏在配置链路、缓存失效、路径归一化这类跨模块流程里，单纯 outcome check 无法约束 agent 必须沿正确边界修复。
- 方案：把复杂任务定义成“验证输出 + 禁改文件 + 至少改动候选根因文件 + read-before-edit + 效率上限”的组合约束；例如缓存题禁止改 `main.py/store.py`，路由题禁止改 `routes.py/scenarios.py`，只允许 agent 在服务层或归一化层完成修复。
- 取舍：任务定义会更啰嗦一些，但评分信号更可信；相比引入重型 sandbox 或 AST grader，这种 schema 级约束成本低、可维护、能直接复用现有 `outcome/trajectory/efficiency` graders。
- 验证：新增 3 组复杂 fixture（配置链路、缓存失效、路径归一化）和对应 task JSON，补 loader 测试后，真实运行 `python -m eval.runner --disable-llm-judge` 可生成完整 trial / report / summary。
- 面试表达：我做 agent benchmark 时专门处理了“投机通过”的问题，不是继续堆一个更聪明的 judge，而是把任务定义本身设计成抗作弊结构：既检查结果，也限制能改什么、必须先读什么、最多花多少步，这样通过才更像真实修 bug。
- 更新时间：2026-06-15

### C044：外部 agent 评测超时如果不杀子进程树，会把 benchmark 基线目录悄悄污染
- 场景：用 Claude Code 这类外部 agent 跑本地 `eval/fixtures/` 题库时，某些实验命令会超时退出。
- 问题：父进程虽然超时返回，但底层 `node ... claude-code/cli.js` 仍在后台继续跑，随后把 fixture 源目录改脏，导致后续 trial 不改代码也会“验证通过”。
- 根因：原 harness 直接用 `subprocess.run(timeout=...)`，超时时没有显式清理整棵进程树；Windows 下包装脚本和子 Node 进程会残留。
- 方案：把外部进程执行统一收敛到 `_run_captured_process()`，超时后在 Windows 上用 `taskkill /T /F` 杀整棵树；同时新增回归测试，验证超时后子进程不会继续写文件，并补一条 fixture 必须保持 failing baseline 的测试。
- 取舍：实现上增加了一层很小的进程执行封装，但换来的是 benchmark 可重复性和目录洁净性；没有引入更重的作业管理器。
- 验证：`tests/test_eval_system.py` 新增 2 条回归测试并通过 `13 passed`；手工排查确认残留 `claude-code` 进程被清掉后，fixture 基线恢复稳定。
- 面试表达：我处理过一个很隐蔽的 agent 基准污染问题，表面看是模型分数异常，根因其实是超时后的外部子进程没被杀干净，继续在后台改题库；最后我把它收敛成统一的进程树回收和基线守护问题。
- 更新时间：2026-06-15

### C045：把外部 agent 接到第三方模型时，重复 benchmark prompt 可能命中“无工具短路缓存”
- 场景：用 Claude Code 驱动第三方模型跑评测题时，同一任务 prompt 会在多个 trial、多个 agent 之间反复出现。
- 问题：Claude Code 在复制后的 trial workspace 里有时只返回第一句文本回复，`steps=1`、`tool_calls=0`、`input_tokens=4`，看起来像 agent 失效。
- 根因：问题不在 workspace 复制本身，而在外部模型/CLI 的缓存键上；相同 prompt 被错误复用成首句纯文本回复，后续 tool loop 没有真正发生。
- 方案：在 Claude provider 层追加仅用于 cache isolation 的唯一 run nonce，放到附加 system prompt，而不是污染任务原始 prompt；同时显式把评测配置里的模型名传给 Claude，避免它偷走本地默认模型。
- 取舍：这样会让每次 eval 少量增加不可缓存 token，但换来的是跨 trial 可重复性；相比改 task 文案或禁用整套缓存，侵入更小。
- 验证：对同一个 copied workspace，原 prompt 会稳定复现 `cache-...` 单轮假回复；只加 nonce 后立即恢复真实 Bash/Read 工具调用，并进入长时修复流程。
- 面试表达：我修过一个很反直觉的 agent 适配问题，表面像“复制目录后 Claude 不会用了”，实际是第三方模型缓存把工具轨迹吞掉了；解法不是继续改 workspace，而是在 provider 协议层做 cache isolation。
- 更新时间：2026-06-15

### C046：Windows 超长路径会让 Claude Code 的项目日志“看得见却读不到”
- 场景：Claude Code 已经在评测题上修复成功并通过验证，但 `trial.json` 里的 `project_log` 仍为空，`tool_calls` 被错误记成 0。
- 问题：如果轨迹统计缺失，Claude Code 就不能作为“稳定可比”的正式评测选手，因为 PASS 结果和执行行为脱钩了。
- 根因：Claude 把日志写到 `.claude/projects/<sanitized workspace path>/<session>.jsonl`；评测输出目录较深时，这个路径在 Windows 上超过常规长度。Python `glob()` 还能枚举到路径名，但 `Path.stat()/read_text()` 会因长路径失败，导致 harness 误判日志不存在。
- 方案：在 `eval/harness.py` 的 Claude 日志回收边界新增最小的 Windows 长路径适配，只在 `stat/read` 时加 `\\\\?\\` 前缀；同时修正日志等待逻辑，已知 `session_id` 时优先等到 project log，而不是先拿到 debug log 就提前返回。
- 取舍：没有把整个项目铺满长路径兼容，也没有强行缩短所有评测目录；只把修复压在 Claude provider 的日志读取边界，影响面最小。
- 验证：`tests/test_eval_system.py` 新增 Claude 日志等待与路径适配测试并通过；真实运行 `python -m eval.runner --task issue_tracker_regression --agent claude_code --disable-llm-judge` 后，`trial.json` 正常写入 `project_log`，`tool_calls` 从 0 恢复为 11。
- 面试表达：我修过一个非常隐蔽的 Windows benchmark 问题，任务其实已经做对了，但轨迹统计全是假象。最后不是去继续调 prompt，而是从文件系统边界定位到“长路径导致日志可枚举但不可读”，把兼容逻辑收敛在 provider 内层，才让评测结果真正可信。
- 更新时间：2026-06-15

### C047：Benchmark 需要剔除 agent 运行时产物，否则“功能做对”也会被误判越界失败
- 场景：三家 agent 在同一道 debug benchmark 上都把测试跑绿，但评测结论却出现“19/19 通过仍 FAIL”。
- 问题：如果把 `.claude/`、`.iceCoder/`、`data/sessions/`、Vitest 缓存等运行时文件也算进 diff，Gate 会把平台副作用误判成项目越界修改。
- 根因：`eval/harness.py` 早期只对 Claude 的 `.claude/` 做了单点特判，没有把“项目代码变更”和“agent/runtime 产物”在抽象层分开。
- 方案：把 diff 过滤提升为统一规则，按路径前缀排除 `.autocode/`、`.claude/`、`.iceCoder/`、`data/sessions/`、`node_modules/.vite/vitest/`，同时保留真实依赖改动的违规检测。
- 取舍：过滤过宽会放过真实作弊，所以没有直接忽略整个 `node_modules/` 或 `data/`，只排除可证明属于运行时缓存/日志的固定前缀。
- 验证：`tests/test_eval_system.py` 新增回归测试并通过 `25 passed`；过滤后 `node_modules/lodash/index.js` 这类真实依赖改动仍会保留在 diff 中。
- 面试表达：我处理过 benchmark 公平性问题，关键不是调 prompt，而是把“平台副作用”和“项目源码变更”在评测协议层拆开，否则功能正确的 agent 也会被门禁误杀。
- 更新时间：2026-06-15

### C048：Benchmark 报告如果把 Gate 和 Judge 混成“平均分”，会失去原始评分体系的可解释性
- 场景：评测系统已经有 Gate 门禁和 LLM judge，但早期汇总时直接把所有 grader 的 `score` 求平均。
- 问题：这样既不等于原设计里的 `Gate(40)+Judge(60)`，也无法表达 `G1=0 封顶 F`、`G1<15 封顶 C` 这类硬规则，导致结果和设计稿口径不一致。
- 根因：报告层没有把“客观成功率”“Gate 原始分”“Judge 六维总分”“Composite 等级”拆成独立概念，而是沿用了单一 `score` 字段。
- 方案：在 `eval/report.py` 中显式重建四层结构：`objective success`、`gate_score`、`judge_score`、`composite+grade`；汇总指标补齐 `S+A rate / avg turns / avg duration / fallback rate`，并把 cross-agent judge 改成独立的 40/60 合成。
- 取舍：为了兼容旧 summary，保留了归一化 `score` 字段，但报告主视图改为显示 `Gate/Judge/Composite/Grade`；没有继续堆更多 grader，重点是把评分协议拉直。
- 验证：`tests/test_eval_system.py` 补到 `27 passed`；其中覆盖 Gate+Judge 合成、Judge 60 分制解析、agent 维度汇总指标。
- 面试表达：我做过一次评测协议层重构，核心不是“换个更强 judge”，而是先把评分数学模型讲清楚并落到代码里，让每个分数都能解释回 Gate、Judge 和最终等级。
- 更新时间：2026-06-15

### C049：Prompt cache 要真正生效，关键不是“记 token”，而是把稳定前缀和动态运行态拆开
- 场景：agent 多轮运行时，每轮都会重发 system prompt、任务状态、todo、恢复提示和项目记忆，累计 prompt token 很高，但从日志里看不出缓存是否命中。
- 问题：如果动态块直接混在 system prompt 里，provider 侧前缀缓存很难稳定命中；同时 compaction 发生后也没有显式统计“缓存分段”。
- 根因：原实现只有总 `prompt_tokens/completion_tokens` 统计，没有 `cache hit/miss` telemetry；消息装配也只有一层，主历史和 API 请求视图没有分开。
- 方案：把提示词拆成 `static_system_prompt + runtime_state_tail`，让 `AGENTS.md/CLAUDE.md` 留在稳定前缀，把 task/todo/recovery/project memory 放到尾部动态块；同时统一解析 `prompt_cache_hit_tokens / prompt_cache_miss_tokens / cached_tokens`，并把 compaction 记录成新的 cache segment。
- 取舍：没有引入双历史存储，也没有做旁路 LLM 前缀复用，只保留一层轻量 request view；这样能力不如重型 runtime 完整，但复杂度明显更低。
- 验证：补了 LLM usage、trace、Feishu live card、request view 分层等回归测试，`pytest -q tests/test_llm.py tests/test_litellm.py tests/test_trace.py tests/test_feishu_remote.py tests/test_core.py tests/test_foundation.py tests/test_remote.py tests/test_llm_rounds.py` 通过 `100 passed`。
- 面试表达：我做 prompt cache 优化时，没有上来就堆复杂缓存层，而是先把消息工程拉直：稳定前缀单独固定，动态状态放尾部，再把 cache hit/miss 和 compaction segment 做成可观测指标，这样既能看到收益，也不会把 runtime 做重。
- 更新时间：2026-06-15

### C050：给轻量 coding agent 接入 MCP，关键是把“工具发现”和“运行时治理”解耦
- 场景：需要给 AutoCode 增加 MCP 能力，但又不想把当前本地 runtime 扩成一整套 plugin/skills 平台。
- 问题：如果直接把 MCP server 生命周期、工具注册、远程入口和子代理各写一套，很快就会出现 CLI、远程聊天、子代理三套行为不一致的问题。
- 根因：原实现的工具集是固定常量 `ALL_TOOLS`，入口层各自拼工具，没有统一的“按配置生成工具集”边界。
- 方案：新增极简 `autocode/mcp.py`，只支持 stdio MCP 和 `tools/list` / `tools/call`；再补一个统一的 `build_agent_tools(config)` 入口，让 CLI、RemoteManager、Feishu 和子代理都通过同一工厂拿到“内置工具 + MCP 工具”。
- 取舍：没有顺手引入 Skills、MCP 资源/Prompt、动态热更新或复杂 supervisor，只保留最小可用的工具发现与调用链路；同时把所有 `mcp_*` 工具默认设为 `confirm`，用现有审批层兜底。
- 验证：新增 fake MCP stdio server 回归测试，验证工具发现、调用、关闭；并补策略测试确认 `mcp_*` 默认进入审批。`pytest -q tests/test_mcp.py tests/test_mcp_policy.py tests/test_remote.py tests/test_core.py tests/test_tools.py tests/test_policy.py` 通过。
- 面试表达：我做 MCP 接入时，没有把 runtime 一次性升级成重平台，而是先找最小稳定边界：统一工具工厂、统一入口复用、统一审批策略。这样先把能力接进来，再决定后面要不要继续长成更重的扩展层。
- 更新时间：2026-06-16

### C051：MCP 同时兼容 JSON 行和 Content-Length 时，半包处理比“能不能解析 JSON”更关键
- 场景：继续把 AutoCode 的 MCP 实现向 iceCoder 靠拢，需要支持共享 `MCPManager`、后台初始化，以及 `Content-Length` 分帧兼容。
- 问题：表面上 `tools/list` / `tools/call` 已经打通，但一接入 `Content-Length` 响应，初始化就会卡死，CLI 看起来像“没有报错但也没准备好”。
- 根因：如果 stdout 第一次只收到 `Content-Length` 半包，旧实现会误走 JSON 行解析，把头部裁掉；另外 Windows 文本流里直接写 `\r\n` 还会发生换行转换，测试假 server 会生成畸形分隔符。
- 方案：把 MCP stdout 读取改成底层字节流读取，先判断 framed message，再决定是否回退到 JSON 行解析；同时让测试 server 在 `Content-Length` 分支走二进制写 stdout，真实覆盖协议边界。
- 取舍：没有引入更重的异步 IO 或 supervisor，只在 `autocode/mcp.py` 内收敛读取和分帧状态机；复杂度仍然低，但已经把最容易卡死的兼容边界补上。
- 验证：新增/修正 `tests/test_mcp.py`，覆盖普通 JSON 行响应和 `Content-Length` framed 响应；再联跑 `tests/test_mcp.py tests/test_mcp_policy.py tests/test_cli.py tests/test_remote.py tests/test_runtime.py` 共 `40 passed`。
- 面试表达：我做协议兼容时不会只停在“能 parse 一个完整包”，而是会把半包、分帧和 Windows 流行为当成第一等边界，因为真正的线上卡死往往就出在这里。
- 更新时间：2026-06-16

