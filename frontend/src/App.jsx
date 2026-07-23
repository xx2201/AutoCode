import {
  Activity,
  ArrowRight,
  Bot,
  Check,
  ChevronDown,
  CircleStop,
  Clock3,
  Code2,
  FileCode2,
  Folder,
  FolderGit2,
  Github,
  History,
  KeyRound,
  LayoutGrid,
  ListTodo,
  LogOut,
  Menu,
  MessageSquareText,
  MoreHorizontal,
  PanelLeftClose,
  Play,
  Plus,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
  X,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const TOKEN_KEY = "autocode_web_token";
const WORKSPACE_KEY = "autocode_workspace_id";
const CLIENT_MAP_KEY = "autocode_workspace_clients";

function createClientId() {
  if (window.crypto?.randomUUID) {
    return `web_${window.crypto.randomUUID().replaceAll("-", "")}`;
  }
  return `web_${Date.now()}_${Math.random().toString(36).slice(2, 14)}`;
}

function clientIdFor(workspaceId) {
  let clients = {};
  try {
    clients = JSON.parse(localStorage.getItem(CLIENT_MAP_KEY) || "{}");
  } catch {
    clients = {};
  }
  if (!clients[workspaceId]) {
    clients[workspaceId] = createClientId();
    localStorage.setItem(CLIENT_MAP_KEY, JSON.stringify(clients));
  }
  return clients[workspaceId];
}

function renewClientId(workspaceId) {
  let clients = {};
  try {
    clients = JSON.parse(localStorage.getItem(CLIENT_MAP_KEY) || "{}");
  } catch {
    clients = {};
  }
  clients[workspaceId] = createClientId();
  localStorage.setItem(CLIENT_MAP_KEY, JSON.stringify(clients));
  return clients[workspaceId];
}

async function request(token, path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function RichText({ content }) {
  const blocks = String(content || "").split("```");
  return (
    <div className="rich-text">
      {blocks.map((block, index) =>
        index % 2 ? (
          <pre key={`${index}-${block.slice(0, 12)}`}>
            <code>{block.replace(/^[a-zA-Z0-9_+-]+\n/, "")}</code>
          </pre>
        ) : (
          block
            .split(/\n{2,}/)
            .filter(Boolean)
            .map((paragraph, paragraphIndex) => (
              <p key={`${index}-${paragraphIndex}`}>
                {paragraph.split("\n").map((line, lineIndex) => (
                  <span key={`${lineIndex}-${line.slice(0, 8)}`}>
                    {line}
                    {lineIndex < paragraph.split("\n").length - 1 && <br />}
                  </span>
                ))}
              </p>
            ))
        ),
      )}
    </div>
  );
}

function ProjectPicker({ open, workspaces, onSelect, onClose, onRefresh, required }) {
  const [query, setQuery] = useState("");
  const inputRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    window.setTimeout(() => inputRef.current?.focus(), 80);
  }, [open]);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    if (!keyword) return workspaces;
    return workspaces.filter(
      (workspace) =>
        workspace.name.toLocaleLowerCase().includes(keyword) ||
        workspace.path.toLocaleLowerCase().includes(keyword),
    );
  }, [query, workspaces]);

  if (!open) return null;

  return (
    <div className="modal-layer project-layer" role="dialog" aria-modal="true">
      <section className="project-picker">
        <div className="project-picker-head">
          <div className="project-picker-mark">
            <FolderGit2 size={24} />
          </div>
          <div>
            <span className="overline">LOCAL PROJECTS</span>
            <h2>选择一个项目开始</h2>
            <p>Agent 会在你选择的本机目录中读取代码、执行命令并保存会话。</p>
          </div>
          <div className="project-picker-actions">
            <button
              className="icon-button"
              type="button"
              onClick={onRefresh}
              aria-label="刷新 CLI Workspace"
              title="刷新 CLI Workspace"
            >
              <RefreshCw size={18} />
            </button>
            {!required && (
              <button className="icon-button" type="button" onClick={onClose} aria-label="关闭">
                <X size={20} />
              </button>
            )}
          </div>
        </div>

        <label className="project-search">
          <Search size={18} />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索项目名称或路径"
          />
          <kbd>{filtered.length}</kbd>
        </label>

        <div className="project-grid">
          {filtered.map((workspace, index) => (
            <button
              className="project-card"
              type="button"
              key={workspace.workspace_id}
              onClick={() => onSelect(workspace)}
            >
              <span className={`project-icon project-tone-${index % 5}`}>
                <Folder size={24} />
              </span>
              <span className="project-copy">
                <strong>{workspace.name}</strong>
                <small>{workspace.path}</small>
              </span>
              <ArrowRight className="project-arrow" size={18} />
            </button>
          ))}
          {!workspaces.length && (
            <div className="empty-projects">
              <TerminalSquare size={28} />
              <strong>CLI 还没有打开过项目</strong>
              <span>先在项目目录运行 autocode，再回到这里刷新。</span>
              <code>cd your-project &amp;&amp; autocode</code>
            </div>
          )}
          {workspaces.length > 0 && !filtered.length && (
            <div className="empty-projects">
              <Search size={28} />
              <strong>没有匹配的项目</strong>
              <span>换个关键词试试</span>
            </div>
          )}
        </div>

        <footer className="project-picker-foot">
          <ShieldCheck size={16} />
          Web 只展示 AutoCode CLI 已打开并注册的本机项目
        </footer>
      </section>
    </div>
  );
}

function LoginView({ onLogin, error, busy }) {
  const [candidate, setCandidate] = useState("");

  return (
    <main className="login-page">
      <div className="login-orb orb-one" />
      <div className="login-orb orb-two" />
      <section className="login-shell">
        <div className="login-story">
          <div className="wordmark">
            <span className="logo-mark">
              <Code2 size={22} />
            </span>
            <strong>AutoCode</strong>
          </div>
          <div className="login-heading">
            <span className="hero-chip">
              <Sparkles size={15} /> YOUR LOCAL AI WORKSPACE
            </span>
            <h1>
              你的项目，
              <br />
              随时从手机继续。
            </h1>
            <p>
              项目、模型和命令都留在你的电脑上。Web 只负责安全地发送任务与呈现结果。
            </p>
          </div>
          <div className="login-features">
            <span><FolderGit2 size={17} /> 按项目切换 workspace</span>
            <span><ShieldCheck size={17} /> 本机执行与审批</span>
            <span><Activity size={17} /> 会话与 Trace</span>
          </div>
        </div>

        <form
          className="login-card"
          onSubmit={(event) => {
            event.preventDefault();
            if (candidate.trim()) onLogin(candidate.trim());
          }}
        >
          <div className="login-card-icon">
            <KeyRound size={24} />
          </div>
          <span className="overline">SECURE ACCESS</span>
          <h2>连接本机 Agent</h2>
          <p>输入部署时生成的浏览器访问令牌。</p>
          <label>
            访问令牌
            <div className="token-input">
              <input
                type="password"
                value={candidate}
                onChange={(event) => setCandidate(event.target.value)}
                placeholder="粘贴访问令牌"
                autoComplete="current-password"
                required
              />
              <ShieldCheck size={18} />
            </div>
          </label>
          {error && <div className="form-error">{error}</div>}
          <button className="primary-action login-action" type="submit" disabled={busy}>
            {busy ? <RefreshCw className="spin" size={18} /> : <ArrowRight size={18} />}
            {busy ? "正在连接" : "进入工作台"}
          </button>
          <small className="privacy-copy">令牌只保存在当前浏览器，不会出现在 URL 中。</small>
        </form>
      </section>
    </main>
  );
}

function Message({ message }) {
  const assistant = message.role === "assistant";
  const tool = message.role === "tool";
  return (
    <article className={`message-row ${message.role}`}>
      <div className="message-avatar">
        {assistant ? <Bot size={18} /> : tool ? <TerminalSquare size={18} /> : "你"}
      </div>
      <div className="message-column">
        <div className="message-label">
          {assistant ? "AutoCode" : tool ? "Tool output" : "You"}
        </div>
        <div className="message-bubble">
          {tool ? <pre>{message.content}</pre> : <RichText content={message.content} />}
        </div>
      </div>
    </article>
  );
}

function Welcome({ workspace, onPrompt }) {
  const suggestions = [
    {
      icon: <LayoutGrid size={20} />,
      title: "理解项目",
      copy: "从入口和依赖关系开始梳理架构",
      prompt: "先阅读当前项目，从真实入口出发给我一个架构概览",
    },
    {
      icon: <Activity size={20} />,
      title: "检查质量",
      copy: "运行测试并指出主要风险",
      prompt: "检查当前项目的测试状态、代码质量和主要风险",
    },
    {
      icon: <Github size={20} />,
      title: "解读改动",
      copy: "查看最近改动并解释影响",
      prompt: "查看当前项目最近的 Git 改动，并解释它们的作用",
    },
  ];
  return (
    <section className="welcome">
      <div className="welcome-symbol">
        <Sparkles size={27} />
      </div>
      <span className="overline">READY IN {workspace?.name?.toUpperCase()}</span>
      <h1>今天想在这个项目里完成什么？</h1>
      <p>
        AutoCode 已连接到 <strong>{workspace?.path}</strong>，可以读取代码、修改文件并运行测试。
      </p>
      <div className="suggestion-grid">
        {suggestions.map((item) => (
          <button type="button" key={item.title} onClick={() => onPrompt(item.prompt)}>
            <span>{item.icon}</span>
            <strong>{item.title}</strong>
            <small>{item.copy}</small>
            <ArrowRight size={16} />
          </button>
        ))}
      </div>
    </section>
  );
}

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [authState, setAuthState] = useState("checking");
  const [authError, setAuthError] = useState("");
  const [bootstrap, setBootstrap] = useState({ model: "", workspaces: [], version: "" });
  const [selectedId, setSelectedId] = useState(
    () => localStorage.getItem(WORKSPACE_KEY) || "",
  );
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [runnerOnline, setRunnerOnline] = useState(false);
  const [messages, setMessages] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [pending, setPending] = useState(null);
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("idle");
  const [toast, setToast] = useState("");
  const [panel, setPanel] = useState(null);
  const [panelContent, setPanelContent] = useState("");
  const messageEndRef = useRef(null);

  const selectedWorkspace = useMemo(
    () => bootstrap.workspaces.find((item) => item.workspace_id === selectedId) || null,
    [bootstrap.workspaces, selectedId],
  );
  const clientId = selectedWorkspace ? clientIdFor(selectedWorkspace.workspace_id) : "";

  const showToast = useCallback((message) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 2800);
  }, []);

  const loadBootstrap = useCallback(
    async (activeToken) => {
      const data = await request(activeToken, "/api/bootstrap");
      setBootstrap(data);
      setRunnerOnline(true);
      const saved = localStorage.getItem(WORKSPACE_KEY);
      const savedExists = data.workspaces.some((item) => item.workspace_id === saved);
      if (savedExists) {
        setSelectedId(saved);
      } else {
        setSelectedId("");
        setProjectPickerOpen(true);
      }
      return data;
    },
    [],
  );

  useEffect(() => {
    let ignore = false;
    async function verifySavedToken() {
      if (!token) {
        setAuthState("logged-out");
        return;
      }
      try {
        await request(token, "/api/auth/verify", { method: "POST" });
        if (ignore) return;
        setAuthState("ready");
        try {
          await loadBootstrap(token);
        } catch (error) {
          if (!ignore) {
            setRunnerOnline(false);
            showToast(error.message);
          }
        }
      } catch {
        if (ignore) return;
        localStorage.removeItem(TOKEN_KEY);
        setToken("");
        setAuthState("logged-out");
        setAuthError("保存的访问令牌已失效，请重新输入。");
      }
    }
    verifySavedToken();
    return () => {
      ignore = true;
    };
  }, [loadBootstrap, showToast, token]);

  const refreshSessions = useCallback(async () => {
    if (!selectedWorkspace || !runnerOnline) return;
    const data = await request(
      token,
      `/api/sessions?workspace_id=${encodeURIComponent(selectedWorkspace.workspace_id)}`,
    );
    setSessions(data.sessions || []);
  }, [runnerOnline, selectedWorkspace, token]);

  useEffect(() => {
    if (!selectedWorkspace) return;
    setMessages([]);
    setPending(null);
    setStatus("idle");
    refreshSessions().catch((error) => showToast(error.message));
  }, [refreshSessions, selectedWorkspace, showToast]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  useEffect(() => {
    if (authState !== "ready") return undefined;
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch("/api/health", { cache: "no-store" });
        const health = await response.json();
        const wasOffline = !runnerOnline;
        setRunnerOnline(Boolean(health.runner_connected));
        if (health.runner_connected && wasOffline) {
          await loadBootstrap(token);
          showToast("本机 Runner 已重新连接");
        }
      } catch {
        setRunnerOnline(false);
      }
    }, 15000);
    return () => window.clearInterval(timer);
  }, [authState, loadBootstrap, runnerOnline, showToast, token]);

  async function login(candidate) {
    setAuthError("");
    setAuthState("checking");
    try {
      await request(candidate, "/api/auth/verify", { method: "POST" });
      localStorage.setItem(TOKEN_KEY, candidate);
      setToken(candidate);
      setAuthState("ready");
      try {
        await loadBootstrap(candidate);
      } catch (error) {
        setRunnerOnline(false);
        showToast(error.message);
      }
    } catch (error) {
      setAuthState("logged-out");
      setAuthError(error.message);
    }
  }

  function selectWorkspace(workspace) {
    localStorage.setItem(WORKSPACE_KEY, workspace.workspace_id);
    setSelectedId(workspace.workspace_id);
    setProjectPickerOpen(false);
    setMobileNavOpen(false);
    showToast(`已打开 ${workspace.name}`);
  }

  async function openProjectPicker() {
    setProjectPickerOpen(true);
    if (!runnerOnline) return;
    try {
      await loadBootstrap(token);
    } catch (error) {
      setRunnerOnline(false);
      showToast(error.message);
    }
  }

  async function submitPrompt(value = prompt) {
    const cleanPrompt = value.trim();
    if (!cleanPrompt || busy || !selectedWorkspace) return;
    if (!runnerOnline) {
      showToast("本机 Runner 离线，暂时不能执行任务。");
      return;
    }
    setMessages((items) => [...items, { role: "user", content: cleanPrompt }]);
    setPrompt("");
    setBusy(true);
    setStatus("running");
    try {
      const result = await request(token, "/api/chat", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          prompt: cleanPrompt,
        }),
      });
      if (result.text) {
        setMessages((items) => [...items, { role: "assistant", content: result.text }]);
      }
      setPending(result.pending_tool ? result : null);
      setStatus(result.status || "completed");
      await refreshSessions();
    } catch (error) {
      setMessages((items) => [
        ...items,
        { role: "assistant", content: `请求失败：${error.message}` },
      ]);
      setStatus("failed");
      if (error.status === 401) logout("访问令牌已失效。");
    } finally {
      setBusy(false);
    }
  }

  async function resolveApproval(action) {
    if (!pending || busy || !selectedWorkspace) return;
    setBusy(true);
    try {
      const result = await request(token, "/api/approval", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          action,
        }),
      });
      if (result.text) {
        setMessages((items) => [...items, { role: "assistant", content: result.text }]);
      }
      setPending(result.pending_tool ? result : null);
      setStatus(result.status || "completed");
      await refreshSessions();
    } catch (error) {
      showToast(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function resumeSession(sessionId) {
    setPanel(null);
    setBusy(true);
    try {
      const data = await request(token, "/api/resume", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          session_id: sessionId,
        }),
      });
      setMessages(data.messages || []);
      setPending(data.result?.pending_tool ? data.result : null);
      setStatus(data.result?.status || "idle");
      showToast("会话已恢复");
    } catch (error) {
      showToast(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function newSession() {
    if (!selectedWorkspace) return;
    try {
      await request(token, "/api/reset", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
        }),
      });
    } catch (error) {
      if (error.status !== 503) showToast(error.message);
    }
    renewClientId(selectedWorkspace.workspace_id);
    setMessages([]);
    setPending(null);
    setStatus("idle");
    setPanel(null);
    showToast("已新建会话");
  }

  async function openInfo(kind) {
    if (!selectedWorkspace) return;
    try {
      const suffix = `workspace_id=${encodeURIComponent(selectedWorkspace.workspace_id)}`;
      const data = await request(token, `/api/${kind}/${clientId}?${suffix}`);
      setPanelContent(data.summary);
      setPanel(kind);
    } catch (error) {
      showToast(error.message);
    }
  }

  function logout(message = "") {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setAuthState("logged-out");
    setAuthError(message);
  }

  if (authState === "checking" && token) {
    return (
      <div className="splash-screen">
        <span className="logo-mark"><Code2 size={23} /></span>
        <RefreshCw className="spin" size={22} />
        <p>正在连接本机 Agent…</p>
      </div>
    );
  }

  if (authState !== "ready") {
    return <LoginView onLogin={login} error={authError} busy={authState === "checking"} />;
  }

  return (
    <div className="app-frame">
      <aside className={`sidebar ${mobileNavOpen ? "mobile-open" : ""}`}>
        <div className="sidebar-brand">
          <span className="logo-mark"><Code2 size={20} /></span>
          <div><strong>AutoCode</strong><small>Local Agent</small></div>
          <button
            className="sidebar-close"
            type="button"
            onClick={() => setMobileNavOpen(false)}
            aria-label="关闭菜单"
          >
            <PanelLeftClose size={19} />
          </button>
        </div>

        <button
          className="workspace-switcher"
          type="button"
          onClick={openProjectPicker}
        >
          <span className="workspace-icon"><FolderGit2 size={19} /></span>
          <span>
            <small>当前项目</small>
            <strong>{selectedWorkspace?.name || "选择项目"}</strong>
          </span>
          <ChevronDown size={17} />
        </button>

        <nav className="side-nav">
          <span className="nav-caption">WORKSPACE</span>
          <button className="active" type="button" onClick={() => setMobileNavOpen(false)}>
            <MessageSquareText size={18} /> 对话
          </button>
          <button type="button" onClick={() => openInfo("task")}>
            <ListTodo size={18} /> 当前任务
          </button>
          <button type="button" onClick={() => openInfo("trace")}>
            <Activity size={18} /> 运行 Trace
          </button>
          <button
            type="button"
            onClick={() => {
              refreshSessions().catch((error) => showToast(error.message));
              setPanel("sessions");
            }}
          >
            <History size={18} /> 历史会话
            {sessions.length > 0 && <em>{sessions.length}</em>}
          </button>
        </nav>

        <div className="sidebar-bottom">
          <div className={`runner-card ${runnerOnline ? "online" : "offline"}`}>
            <span className="runner-dot" />
            <div>
              <strong>{runnerOnline ? "本机 Runner 在线" : "本机 Runner 离线"}</strong>
              <small>{runnerOnline ? "项目能力已就绪" : "等待电脑重新连接"}</small>
            </div>
          </div>
          <button className="logout-link" type="button" onClick={() => logout()}>
            <LogOut size={17} /> 退出登录
          </button>
        </div>
      </aside>

      {mobileNavOpen && (
        <button
          className="mobile-backdrop"
          type="button"
          aria-label="关闭菜单"
          onClick={() => setMobileNavOpen(false)}
        />
      )}

      <main className="workspace-main">
        <header className="workspace-header">
          <button
            className="mobile-menu"
            type="button"
            onClick={() => setMobileNavOpen(true)}
            aria-label="打开菜单"
          >
            <Menu size={21} />
          </button>
          <div className="workspace-title">
            <div className="project-mini-icon"><FileCode2 size={20} /></div>
            <div>
              <strong>{selectedWorkspace?.name || "尚未选择项目"}</strong>
              <small>{selectedWorkspace?.path || "从项目列表中打开一个 workspace"}</small>
            </div>
          </div>
          <div className="header-meta">
            <span className={`connection-pill ${runnerOnline ? "online" : "offline"}`}>
              <i /> {runnerOnline ? "Connected" : "Offline"}
            </span>
            <span className="model-pill"><Zap size={14} /> {bootstrap.model || "model"}</span>
            <button className="icon-button" type="button" onClick={() => openInfo("trace")}>
              <MoreHorizontal size={20} />
            </button>
          </div>
        </header>

        <section className="conversation">
          <div className="conversation-inner">
            {!selectedWorkspace ? (
              <div className="select-project-empty">
                <FolderGit2 size={36} />
                <h1>先打开一个本机项目</h1>
                <p>CoreCoder 是 Agent 引擎，真正的 workspace 由你选择。</p>
                <button className="primary-action" type="button" onClick={openProjectPicker}>
                  <Folder size={18} /> 选择项目
                </button>
              </div>
            ) : messages.length === 0 ? (
              <Welcome workspace={selectedWorkspace} onPrompt={submitPrompt} />
            ) : (
              <div className="message-list">
                {messages.map((message, index) => (
                  <Message key={`${message.role}-${index}`} message={message} />
                ))}
                {busy && (
                  <article className="message-row assistant">
                    <div className="message-avatar"><Bot size={18} /></div>
                    <div className="message-column">
                      <div className="message-label">AutoCode</div>
                      <div className="thinking"><i /><i /><i /><span>正在处理</span></div>
                    </div>
                  </article>
                )}
                <div ref={messageEndRef} />
              </div>
            )}
          </div>
        </section>

        {pending && (
          <section className="approval-banner">
            <div className="approval-icon"><ShieldCheck size={21} /></div>
            <div className="approval-copy">
              <strong>需要你的确认</strong>
              <span>{pending.pending_tool} · {pending.pending_reason || "此操作需要审批"}</span>
              <code>
                {pending.pending_arguments?.command ||
                  JSON.stringify(pending.pending_arguments || {}, null, 2)}
              </code>
            </div>
            <div className="approval-actions">
              <button type="button" onClick={() => resolveApproval("reject")}>
                <CircleStop size={16} /> 拒绝
              </button>
              <button type="button" onClick={() => resolveApproval("approve")}>
                <Check size={16} /> 批准一次
              </button>
              {!pending.pending_requires_manual && (
                <button className="approve-primary" type="button" onClick={() => resolveApproval("approve_all")}>
                  <Play size={16} /> 批准后续普通操作
                </button>
              )}
            </div>
          </section>
        )}

        <footer className="composer-area">
          <div className="composer">
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  submitPrompt();
                }
              }}
              rows={1}
              maxLength={32000}
              placeholder={
                selectedWorkspace
                  ? `在 ${selectedWorkspace.name} 中告诉 AutoCode 你想完成什么…`
                  : "请先选择项目"
              }
              disabled={!selectedWorkspace || !runnerOnline || busy}
            />
            <div className="composer-bottom">
              <span>
                <TerminalSquare size={15} />
                {status === "running" ? "Agent 正在运行" : "本机安全执行"}
              </span>
              <button
                type="button"
                onClick={() => submitPrompt()}
                disabled={!prompt.trim() || busy || !runnerOnline || !selectedWorkspace}
                aria-label="发送"
              >
                {busy ? <RefreshCw className="spin" size={18} /> : <Send size={18} />}
              </button>
            </div>
          </div>
          <p>Enter 发送 · Shift + Enter 换行 · 危险操作会等待确认</p>
        </footer>
      </main>

      <ProjectPicker
        open={projectPickerOpen}
        workspaces={bootstrap.workspaces}
        onSelect={selectWorkspace}
        onClose={() => setProjectPickerOpen(false)}
        onRefresh={openProjectPicker}
        required={!selectedWorkspace}
      />

      {panel && (
        <div className="modal-layer" role="dialog" aria-modal="true">
          <section className="info-panel">
            <header>
              <div>
                <span className="overline">
                  {panel === "sessions" ? "SESSION HISTORY" : "RUNTIME DETAILS"}
                </span>
                <h2>
                  {panel === "sessions" ? "历史会话" : panel === "trace" ? "运行 Trace" : "当前任务"}
                </h2>
              </div>
              <button className="icon-button" type="button" onClick={() => setPanel(null)}>
                <X size={20} />
              </button>
            </header>
            {panel === "sessions" ? (
              <div className="session-content">
                <button className="new-session" type="button" onClick={newSession}>
                  <Plus size={18} /> 新建会话
                </button>
                <div className="session-list">
                  {sessions.map((session) => (
                    <button
                      type="button"
                      key={session.session_id}
                      onClick={() => resumeSession(session.session_id)}
                    >
                      <span className="session-icon"><MessageSquareText size={18} /></span>
                      <span>
                        <strong>{session.title || session.session_id}</strong>
                        <small>
                          <Clock3 size={13} />
                          {session.saved_at} · step {session.step_index}
                        </small>
                      </span>
                      <ArrowRight size={16} />
                    </button>
                  ))}
                  {!sessions.length && <div className="panel-empty">这个项目还没有可恢复的会话。</div>}
                </div>
              </div>
            ) : (
              <pre className="runtime-content">{panelContent}</pre>
            )}
          </section>
        </div>
      )}

      {toast && <div className="toast"><Check size={16} /> {toast}</div>}
    </div>
  );
}
