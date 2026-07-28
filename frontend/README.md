# AutoCode Web 前端

前端按“应用编排 + 业务功能 + 基础设施”组织，避免把状态、网络请求和视图继续堆回 `App.jsx`。

## 目录边界

```text
src/
├─ api/                 HTTP、SSE 和传输编码
├─ app/                 浏览器存储、Toast 等应用级基础设施
├─ features/            按业务能力组织的组件、Hook 和领域模型
│  ├─ agent-run/        一次 Agent 运行的状态机和事件投影
│  ├─ approvals/        工具审批
│  ├─ attachments/      输入附件
│  ├─ auth/             登录
│  ├─ conversation/     对话、Work 时间线和输入区
│  ├─ files/            项目文件、Git 状态和输出文件
│  ├─ layout/           工作台外壳
│  ├─ sessions/         历史会话
│  └─ workspaces/       项目选择、初始化和上下文用量
├─ styles/              与上述界面区域对应的样式
├─ App.jsx              跨功能工作流编排
└─ main.jsx             React 入口
```

## 依赖方向

- `api` 和 `app` 不依赖 React 视图。
- `features` 可以依赖 `api`、`app` 和稳定的领域工具，不反向依赖 `App.jsx`。
- `App.jsx` 负责组合功能及少量跨域工作流，不实现可复用视图、协议解析或浏览器存储。
- SSE 事件统一经过 `features/agent-run/events.js`，运行状态统一由 reducer 管理。
- 服务端同步后的消息统一经过 `features/conversation/model.js` 合并，避免不同入口产生不同历史结构。

## 新功能放置原则

1. 先判断属于哪个业务功能；组件、Hook、测试放在同一功能边界附近。
2. 网络协议只在 `api` 或对应功能 Hook 中处理，不在 JSX 组件中直接散落 `fetch`。
3. 三个及以上相关状态、或具有明确状态迁移的流程，优先使用 reducer。
4. 能从现有 state/props 推导的数据不重复保存。
5. 样式加入对应的 `styles/*.css`；`styles.css` 只维护稳定的加载顺序。

## 验证

```powershell
npm test
npm run build
```

前端结构测试会独立编译主要 JSX 模块，并检查会话恢复、SSE 事件、审批、Markdown、Diff 和响应式样式的关键契约。
