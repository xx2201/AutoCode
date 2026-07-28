import {
  Activity,
  ArrowRight,
  Check,
  ChevronDown,
  Clock3,
  Code2,
  Download,
  ExternalLink,
  FileCode2,
  FileDiff,
  Folder,
  FolderGit2,
  GitBranch,
  Github,
  History,
  KeyRound,
  LayoutGrid,
  ListTodo,
  LogOut,
  Menu,
  MessageSquareText,
  Paperclip,
  Pencil,
  PanelLeftClose,
  Plus,
  RefreshCw,
  Redo2,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
  Trash2,
  Undo2,
  FileText,
  Image as ImageIcon,
  X,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import FilePanel from "./FilePanel";
import GitPanel from "./GitPanel";
import RichText from "./markdown";
import { approvalPresentation } from "./approval";
import {
  clearPageSessionId,
  createSessionRequestCoordinator,
  readPageSessionId,
  storePageSessionId,
} from "./session-history";
import {
  createPendingInput,
  formatDuration,
  formatToolTitle,
  groupConversation,
  latestCompletedTurnId,
  mergeWorkEvent,
  normalizeChangeAction,
  settlePendingInput,
} from "./conversation";

const TOKEN_KEY = "autocode_web_token";
const WORKSPACE_KEY = "autocode_workspace_id";
const CLIENT_MAP_KEY = "autocode_workspace_clients";
const PERMISSION_MODE_KEY = "autocode_permission_mode";

function createClientId() {
  if (window.crypto?.randomUUID) {
    return `web_${window.crypto.randomUUID().replaceAll("-", "")}`;
  }
  return `web_${Date.now()}_${Math.random().toString(36).slice(2, 14)}`;
}

function createInteractionId(prefix) {
  if (window.crypto?.randomUUID) return `${prefix}_${window.crypto.randomUUID()}`;
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

function formatFileSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function attachChangedFilesToLatestTurn(messages, changedFiles = []) {
  if (changedFiles.length === 0) return messages;
  const latestUser = messages.findLastIndex((message) => message.role === "user");
  if (latestUser < 0) return messages;
  const next = [...messages];
  next[latestUser] = {
    ...next[latestUser],
    changed_files: changedFiles,
  };
  return next;
}

function ApprovalRequest({ pending, busy, position, total, onResolve }) {
  const view = approvalPresentation(pending);
  const titleId = `approval-request-title-${pending.approval_id || position}`;
  return (
    <section
      className={`approval-request approval-${view.tone}`}
      aria-labelledby={titleId}
    >
      <header className="approval-request-head">
        <span className="approval-request-mark"><ShieldCheck size={17} /></span>
        <div>
          <small>需要确认{total > 1 ? ` · ${position}/${total}` : ""}</small>
          <h3 id={titleId}>{view.title}</h3>
        </div>
      </header>

      <p className="approval-request-summary">{view.summary}</p>

      <div className="approval-request-target">
        <span>{view.targetLabel}</span>
        <code>{view.target}</code>
      </div>

      <div className="approval-request-risk">
        <ShieldCheck size={14} />
        <span>{view.reason}</span>
      </div>

      <details className="approval-request-details">
        <summary>
          <ChevronDown size={15} />
          {view.detailLabel}
        </summary>
        <pre>{view.detail}</pre>
      </details>

      <footer className="approval-request-footer">
        <p>{view.note}</p>
        <div className="approval-request-actions">
          <button type="button" disabled={busy} onClick={() => onResolve("reject")}>
            拒绝
          </button>
          <button
            className="approval-allow"
            type="button"
            disabled={busy}
            onClick={() => onResolve("approve")}
          >
            {view.allowLabel}
          </button>
          {(pending.pending_approval_scope || pending.approval_scope) && (
            <button
              className="approval-allow"
              type="button"
              disabled={busy}
              onClick={() => onResolve("approve_scope")}
            >
              {view.allowAllLabel}
            </button>
          )}
        </div>
      </footer>
    </section>
  );
}

function hasPendingApprovals(result) {
  return Boolean(
    result?.pending_tool
    || result?.pending_approvals?.some((item) => item.decision === "pending"),
  );
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
  const {
    timeoutMs = 0,
    timeoutMessage = "请求超时，请稍后重试。",
    ...fetchOptions
  } = options;
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const controller = timeoutMs > 0 ? new AbortController() : null;
  const timeoutId = controller
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : null;
  try {
    const response = await fetch(path, {
      ...fetchOptions,
      headers,
      signal: controller?.signal || fetchOptions.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || `请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeoutError = new Error(timeoutMessage);
      timeoutError.status = 408;
      throw timeoutError;
    }
    throw error;
  } finally {
    if (timeoutId !== null) window.clearTimeout(timeoutId);
  }
}

function requestSessionResume(token, {
  clientId,
  workspaceId,
  sessionId,
  permissionMode,
}) {
  return request(token, "/api/resume", {
    method: "POST",
    timeoutMs: 20_000,
    timeoutMessage: "恢复会话超时；本机任务可能仍在运行，请稍后重试。",
    body: JSON.stringify({
      client_id: clientId,
      workspace_id: workspaceId,
      session_id: sessionId,
      permission_mode: permissionMode,
    }),
  });
}

function isTransientRestoreError(error) {
  return !error.status || [408, 409, 429, 502, 503, 504].includes(error.status);
}

async function streamRequest(token, path, payload, onEvent) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  if (!response.body) throw new Error("浏览器不支持流式响应。");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const dataLine = frame
        .split("\n")
        .find((line) => line.startsWith("data: "));
      if (dataLine) onEvent(JSON.parse(dataLine.slice(6)));
    }
    if (done) break;
  }
}

async function encodeAttachment(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return {
    name: file.name,
    media_type: file.type || "application/octet-stream",
    data_base64: window.btoa(binary),
  };
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

function MessageAttachments({ attachments = [] }) {
  if (attachments.length === 0) return null;
  return (
    <div className="message-attachments">
      {attachments.map((attachment) => (
        <span key={`${attachment.name}-${attachment.size}`}>
          {attachment.media_type.startsWith("image/") ? (
            <ImageIcon size={14} />
          ) : (
            <FileText size={14} />
          )}
          {attachment.name}
        </span>
      ))}
    </div>
  );
}

function OutputFiles({ files = [], onFileAction, downloadingFileId }) {
  if (files.length === 0) return null;
  return (
    <div className="output-files">
      {files.map((file) => (
        <div className="output-file" key={file.file_id}>
          <span className="output-file-icon">
            {file.media_type.startsWith("image/") ? (
              <ImageIcon size={18} />
            ) : (
              <FileText size={18} />
            )}
          </span>
          <span className="output-file-copy">
            <strong title={file.name}>{file.name}</strong>
            <small>{formatFileSize(file.size)}</small>
          </span>
          <span className="output-file-actions">
            {file.can_preview && (
              <button
                type="button"
                title="在新页面查看"
                onClick={() => onFileAction(file, true)}
                disabled={downloadingFileId === file.file_id}
              >
                <ExternalLink size={15} />
                查看
              </button>
            )}
            <button
              type="button"
              title="下载到当前设备"
              onClick={() => onFileAction(file, false)}
              disabled={downloadingFileId === file.file_id}
            >
              {downloadingFileId === file.file_id ? (
                <RefreshCw className="spin" size={15} />
              ) : (
                <Download size={15} />
              )}
              下载
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}

function UserMessage({ message, editable, editing, editDraft, editBusy, onEdit, onEditDraft, onCancelEdit, onSaveEdit }) {
  if (!message) return null;
  return (
    <article className="turn-user">
      <div className="user-bubble">
        {editing ? (
          <form className="turn-edit-form" onSubmit={onSaveEdit}>
            <label htmlFor={`edit-${message.turn_id || "latest"}`}>编辑你的提问</label>
            <textarea
              id={`edit-${message.turn_id || "latest"}`}
              value={editDraft}
              onChange={(event) => onEditDraft(event.target.value)}
              rows={3}
              maxLength={32000}
              autoFocus
              disabled={editBusy}
            />
            <p>重新回答只修改对话上下文，不会自动撤销工作区文件。</p>
            <div>
              <button type="button" onClick={onCancelEdit} disabled={editBusy}>取消</button>
              <button className="primary-edit-action" type="submit" disabled={!editDraft.trim() || editBusy}>
                {editBusy ? <RefreshCw className="spin" size={14} /> : <Send size={14} />}
                重新回答
              </button>
            </div>
          </form>
        ) : (
          <>
            <RichText content={message.content} />
            <MessageAttachments attachments={message.attachments} />
            {editable && (
              <button className="turn-edit-button" type="button" onClick={onEdit} aria-label="编辑上一次提问" title="编辑并重新回答">
                <Pencil size={14} />
              </button>
            )}
          </>
        )}
      </div>
    </article>
  );
}

function WorkItem({ item }) {
  const title = formatToolTitle(item.toolName, item.arguments);
  const meta = item.durationMs ? ` · ${formatDuration(item.durationMs)}` : "";
  if (!item.content) {
    return (
      <div className={`work-item-summary ${item.status === "started" ? "is-running" : ""}`}>
        <TerminalSquare size={16} />
        <span>{title}{meta}</span>
        {item.status === "started" && <i className="work-pulse" />}
      </div>
    );
  }
  return (
    <details className="work-item">
      <summary>
        <TerminalSquare size={16} />
        <span>{title}{meta}</span>
        <ChevronDown size={15} />
      </summary>
      <div className="work-output">
        <span>Tool output</span>
        <pre>{item.content}</pre>
      </div>
    </details>
  );
}

function LiveElapsed({ startedAt }) {
  const [elapsedMs, setElapsedMs] = useState(() => Math.max(0, Date.now() - startedAt));

  useEffect(() => {
    const updateElapsed = () => setElapsedMs(Math.max(0, Date.now() - startedAt));
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 250);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  return <span>{`Working for ${formatDuration(elapsedMs)}`}</span>;
}

function WorkBlock({ items, elapsedMs, active = false, liveText = "", stage = "", startedAt = 0 }) {
  const hasContent = items.length > 0 || liveText || active;
  if (!hasContent) return null;
  const stageText = {
    queued: "等待本机领取",
    claimed: "本机已领取",
    runner_started: "启动 Agent",
    model_started: "模型思考中",
    tool_started: "执行工具",
    persisted: "保存会话",
  }[stage] || "正在处理";
  const workLabel = elapsedMs > 0 ? `Worked for ${formatDuration(elapsedMs)}` : "Worked";
  return (
    <details className={`work-block ${active ? "is-active" : ""}`} open={active}>
      <summary>
        {active ? <LiveElapsed startedAt={startedAt} /> : <span>{workLabel}</span>}
        {active && <i className="work-pulse" />}
        <ChevronDown size={16} />
      </summary>
      <div className="work-content">
        {items.map((item) => (
          item.type === "narrative" ? (
            <div className="work-narrative" key={item.id}>
              <RichText content={item.content} />
            </div>
          ) : (
            <WorkItem item={item} key={item.id} />
          )
        ))}
        {liveText && (
          <div className="work-narrative">
            <RichText content={liveText} />
          </div>
        )}
        {active && !liveText && items.length === 0 && (
          <div className="work-stage">
            <i /><i /><i />
            <span>{stageText}</span>
          </div>
        )}
      </div>
    </details>
  );
}

function TurnChangedFiles({ files = [], actionState, onOpenChanges, onChangeAction }) {
  const [expanded, setExpanded] = useState(true);
  if (files.length === 0) return null;
  const persistedState = {
    state: files[0]?.state || "",
    canUndo: files[0]?.can_undo,
    canReapply: files[0]?.can_reapply,
    detail: files[0]?.blocked_reason || "",
  };
  const effectiveState = actionState || persistedState;
  const additions = files.reduce((total, file) => total + Number(file.additions || 0), 0);
  const deletions = files.reduce((total, file) => total + Number(file.deletions || 0), 0);
  const visibleFiles = files.slice(0, 3);
  const remaining = files.length - visibleFiles.length;
  return (
    <details
      className="turn-changes"
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>
        <span>已更改 {files.length} 个文件</span>
        <b>+{additions}</b>
        <i>−{deletions}</i>
        <ChevronDown size={17} />
      </summary>
      <div className="turn-change-list">
        {visibleFiles.map((file) => (
          <button type="button" onClick={() => onOpenChanges(file.path)} key={file.path}>
            <FileCode2 size={16} />
            <span title={file.path}>{file.path}</span>
            <b>+{file.additions || 0}</b>
            <i>−{file.deletions || 0}</i>
          </button>
        ))}
        {remaining > 0 && (
          <button className="turn-changes-more" type="button" onClick={onOpenChanges}>
            查看另外 {remaining} 个文件
            <ArrowRight size={16} />
          </button>
        )}
        <div className="turn-change-actions" role="group" aria-label="文件修改操作">
          <button
            type="button"
            onClick={() => onChangeAction("undo")}
            disabled={effectiveState.busy || effectiveState.canUndo === false || effectiveState.state === "undone"}
          >
            {effectiveState.busyAction === "undo" ? <RefreshCw className="spin" size={14} /> : <Undo2 size={14} />}
            Undo
          </button>
          <button
            type="button"
            onClick={() => onChangeAction("reapply")}
            disabled={effectiveState.busy || effectiveState.canReapply === false || effectiveState.state !== "undone"}
          >
            {effectiveState.busyAction === "reapply" ? <RefreshCw className="spin" size={14} /> : <Redo2 size={14} />}
            Reapply
          </button>
          <span className={`change-action-state ${effectiveState.conflict ? "is-conflict" : ""}`} aria-live="polite">
            {effectiveState.conflict
              ? `冲突：${effectiveState.detail || "文件已发生后续修改"}`
              : effectiveState.state === "undone"
                ? "本轮修改已撤销"
                : effectiveState.detail || ""}
          </span>
        </div>
      </div>
    </details>
  );
}

function AssistantAnswer({
  message,
  changedFiles,
  onFileAction,
  onOpenChanges,
  downloadingFileId,
  actionState,
  onChangeAction,
}) {
  if (!message) return null;
  return (
    <article className="turn-answer">
      <RichText content={message.content} />
      <OutputFiles
        files={message.files}
        onFileAction={onFileAction}
        downloadingFileId={downloadingFileId}
      />
      <TurnChangedFiles
        files={changedFiles}
        actionState={actionState}
        onOpenChanges={onOpenChanges}
        onChangeAction={onChangeAction}
      />
    </article>
  );
}

function ConversationTurn({
  turn,
  active,
  liveWork,
  liveText,
  liveStartedAt,
  stage,
  onFileAction,
  onOpenChanges,
  downloadingFileId,
  editable,
  editing,
  editDraft,
  editBusy,
  onEdit,
  onEditDraft,
  onCancelEdit,
  onSaveEdit,
  actionState,
  onChangeAction,
}) {
  return (
    <section className="conversation-turn">
      <UserMessage
        message={turn.user}
        editable={editable}
        editing={editing}
        editDraft={editDraft}
        editBusy={editBusy}
        onEdit={onEdit}
        onEditDraft={onEditDraft}
        onCancelEdit={onCancelEdit}
        onSaveEdit={onSaveEdit}
      />
      <div className="turn-response">
        <WorkBlock
          items={active ? liveWork : turn.work}
          elapsedMs={turn.elapsedMs}
          active={active}
          liveText={active ? liveText : ""}
          stage={stage}
          startedAt={active ? liveStartedAt : 0}
        />
        {!active && (
          <AssistantAnswer
            message={turn.answer}
            changedFiles={turn.changedFiles}
            onFileAction={onFileAction}
            onOpenChanges={onOpenChanges}
            downloadingFileId={downloadingFileId}
            actionState={actionState}
            onChangeAction={onChangeAction}
          />
        )}
      </div>
    </section>
  );
}

function PendingInputs({ items }) {
  if (items.length === 0) return null;
  return (
    <section className="pending-inputs" aria-label="运行中发送的消息" aria-live="polite">
      {items.map((item) => (
        <article className={`pending-input pending-${item.status}`} key={item.id}>
          <span>{item.mode === "queue" ? "下一轮" : "引导当前回答"}</span>
          <p>{item.prompt}</p>
          <small>
            {item.status === "sending"
              ? "正在发送…"
              : item.status === "failed" || item.status === "rejected"
                ? `发送失败${item.detail ? `：${item.detail}` : ""}`
                : item.detail || "已接收"}
          </small>
        </article>
      ))}
    </section>
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

function formatContextTokens(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(value || 0);
}

function ContextMeter({ usage }) {
  const windowTokens = Math.max(0, usage.window_tokens || 0);
  const usedTokens = Math.min(Math.max(0, usage.used_tokens || 0), windowTokens || Infinity);
  const usedPercent = windowTokens ? Math.min(100, usedTokens / windowTokens * 100) : 0;
  const remainingPercent = Math.max(0, 100 - usedPercent);
  const radius = 8;
  const circumference = 2 * Math.PI * radius;

  return (
    <details className={`context-meter ${usedPercent >= 80 ? "is-high" : ""}`}>
      <summary
        aria-label={`上下文窗口已使用 ${usedPercent.toFixed(0)}%`}
        title="上下文窗口"
      >
        <svg viewBox="0 0 22 22" aria-hidden="true">
          <circle className="context-ring-track" cx="11" cy="11" r={radius} />
          <circle
            className="context-ring-value"
            cx="11"
            cy="11"
            r={radius}
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - usedPercent / 100)}
          />
        </svg>
      </summary>
      <div className="context-popover">
        <strong>Context window:</strong>
        <span>
          {usedPercent.toFixed(0)}% used ({remainingPercent.toFixed(0)}% left)
        </span>
        <small>
          {formatContextTokens(usedTokens)} / {formatContextTokens(windowTokens)} tokens used
        </small>
      </div>
    </details>
  );
}

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [authState, setAuthState] = useState("checking");
  const [authError, setAuthError] = useState("");
  const [bootstrap, setBootstrap] = useState({
    model: "",
    workspaces: [],
    version: "",
    context_window_tokens: 0,
  });
  const [selectedId, setSelectedId] = useState(
    () => localStorage.getItem(WORKSPACE_KEY) || "",
  );
  const [permissionMode, setPermissionMode] = useState(
    () => localStorage.getItem(PERMISSION_MODE_KEY) || "ask",
  );
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [runnerOnline, setRunnerOnline] = useState(false);
  const [messages, setMessages] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [pending, setPending] = useState(null);
  const [approvalBusyIds, setApprovalBusyIds] = useState({});
  const [prompt, setPrompt] = useState("");
  const [attachments, setAttachments] = useState([]);
  const [busy, setBusy] = useState(false);
  const [resumingSessionId, setResumingSessionId] = useState("");
  const [activeSessionId, setActiveSessionId] = useState("");
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deletingSessionId, setDeletingSessionId] = useState("");
  const [status, setStatus] = useState("idle");
  const [streamText, setStreamText] = useState("");
  const streamTextRef = useRef("");
  const [runStage, setRunStage] = useState("");
  const [liveWork, setLiveWork] = useState([]);
  const [runStartedAt, setRunStartedAt] = useState(0);
  const [activeTurnId, setActiveTurnId] = useState("");
  const [deliveryMode, setDeliveryMode] = useState("steer");
  const [pendingInputs, setPendingInputs] = useState([]);
  const [editingTurnId, setEditingTurnId] = useState("");
  const [editDraft, setEditDraft] = useState("");
  const [editBusy, setEditBusy] = useState(false);
  const [changeActionStates, setChangeActionStates] = useState({});
  const [lastTimings, setLastTimings] = useState(null);
  const [contextUsage, setContextUsage] = useState({
    used_tokens: 0,
    window_tokens: 0,
  });
  const [downloadingFileId, setDownloadingFileId] = useState("");
  const [gitState, setGitState] = useState(null);
  const [gitDiff, setGitDiff] = useState(null);
  const [gitOpen, setGitOpen] = useState(false);
  const [gitView, setGitView] = useState("changes");
  const [gitLoading, setGitLoading] = useState(false);
  const [gitBusy, setGitBusy] = useState(false);
  const [filePanelOpen, setFilePanelOpen] = useState(false);
  const [filePanelMode, setFilePanelMode] = useState("changed");
  const [projectFiles, setProjectFiles] = useState([]);
  const [projectFilesTruncated, setProjectFilesTruncated] = useState(false);
  const [selectedFilePath, setSelectedFilePath] = useState("");
  const [selectedFile, setSelectedFile] = useState(null);
  const [filePanelLoading, setFilePanelLoading] = useState(false);
  const [toast, setToast] = useState("");
  const [panel, setPanel] = useState(null);
  const [panelContent, setPanelContent] = useState("");
  const messageEndRef = useRef(null);
  const fileInputRef = useRef(null);
  const promptInputRef = useRef(null);
  const sessionRequestsRef = useRef(null);
  if (!sessionRequestsRef.current) {
    sessionRequestsRef.current = createSessionRequestCoordinator(selectedId);
  }

  const selectedWorkspace = useMemo(
    () => bootstrap.workspaces.find((item) => item.workspace_id === selectedId) || null,
    [bootstrap.workspaces, selectedId],
  );
  const clientId = selectedWorkspace ? clientIdFor(selectedWorkspace.workspace_id) : "";
  const conversationTurns = useMemo(() => groupConversation(messages), [messages]);
  const editableTurnId = useMemo(
    () => latestCompletedTurnId(conversationTurns, busy),
    [busy, conversationTurns],
  );

  const showToast = useCallback((message) => {
    setToast(message);
    window.setTimeout(() => setToast(""), 2800);
  }, []);

  const applyResumedSession = useCallback((workspaceId, sessionId, data) => {
    setMessages(data.messages || []);
    setPending(hasPendingApprovals(data.result) ? data.result : null);
    setStatus(data.result?.status || "idle");
    setContextUsage({
      used_tokens: data.result?.context_used_tokens || 0,
      window_tokens:
        data.result?.context_window_tokens
        || bootstrap.context_window_tokens
        || 0,
    });
    setActiveSessionId(sessionId);
    storePageSessionId(window.history, workspaceId, sessionId);
    setPanel(null);
  }, [bootstrap.context_window_tokens]);

  const loadBootstrap = useCallback(
    async (activeToken) => {
      const data = await request(activeToken, "/api/bootstrap");
      setBootstrap(data);
      setContextUsage((current) => ({
        used_tokens: current.used_tokens,
        window_tokens: data.context_window_tokens || current.window_tokens,
      }));
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
    if (!selectedWorkspace || !runnerOnline) {
      setSessionsLoading(false);
      return [];
    }
    const workspaceId = selectedWorkspace.workspace_id;
    const requestTicket = sessionRequestsRef.current.begin(workspaceId);
    setSessionsLoading(true);
    try {
      const data = await request(
        token,
        `/api/sessions?workspace_id=${encodeURIComponent(workspaceId)}`,
      );
      if (!sessionRequestsRef.current.isCurrent(requestTicket)) return [];
      const nextSessions = data.sessions || [];
      setSessions(nextSessions);
      return nextSessions;
    } catch (error) {
      if (!sessionRequestsRef.current.isCurrent(requestTicket)) return [];
      throw error;
    } finally {
      if (sessionRequestsRef.current.isCurrent(requestTicket)) {
        setSessionsLoading(false);
      }
    }
  }, [runnerOnline, selectedWorkspace, token]);

  const refreshMessages = useCallback(async () => {
    if (!selectedWorkspace || !runnerOnline) return [];
    const data = await request(
      token,
      `/api/messages/${clientId}?workspace_id=${encodeURIComponent(selectedWorkspace.workspace_id)}`,
    );
    return data.messages || [];
  }, [clientId, runnerOnline, selectedWorkspace, token]);

  const refreshGit = useCallback(async () => {
    if (!selectedWorkspace || !runnerOnline) return null;
    setGitLoading(true);
    try {
      const data = await request(
        token,
        `/api/git/status?workspace_id=${encodeURIComponent(selectedWorkspace.workspace_id)}`,
      );
      setGitState(data);
      return data;
    } finally {
      setGitLoading(false);
    }
  }, [runnerOnline, selectedWorkspace, token]);

  useEffect(() => {
    if (!selectedWorkspace) return;
    let ignore = false;
    async function initializeWorkspace() {
      const workspaceId = selectedWorkspace.workspace_id;
      sessionRequestsRef.current.selectWorkspace(workspaceId);
      const restoreClientId = renewClientId(workspaceId);
      setMessages([]);
      setSessions([]);
      setSessionsLoading(true);
      setPendingInputs([]);
      setEditingTurnId("");
      setChangeActionStates({});
      setPending(null);
      setStatus("idle");
      setContextUsage({
        used_tokens: 0,
        window_tokens: bootstrap.context_window_tokens || 0,
      });
      setActiveSessionId("");
      try {
        const [availableSessions] = await Promise.all([refreshSessions(), refreshGit()]);
        if (ignore) return;
        const pageSessionId = readPageSessionId(window.history, workspaceId);
        if (!pageSessionId) return;
        if (!availableSessions.some((session) => session.session_id === pageSessionId)) {
          clearPageSessionId(window.history);
          return;
        }
        setBusy(true);
        setResumingSessionId(pageSessionId);
        try {
          const data = await requestSessionResume(token, {
            clientId: restoreClientId,
            workspaceId,
            sessionId: pageSessionId,
            permissionMode,
          });
          if (ignore) return;
          applyResumedSession(workspaceId, pageSessionId, data);
          showToast("已恢复上次会话");
        } catch (error) {
          if (ignore) return;
          if (!isTransientRestoreError(error)) {
            clearPageSessionId(window.history);
          }
          showToast(`自动恢复会话失败：${error.message}`);
        } finally {
          if (!ignore) {
            setResumingSessionId("");
            setBusy(false);
          }
        }
      } catch (error) {
        if (!ignore) showToast(error.message);
      }
    }
    initializeWorkspace();
    return () => {
      ignore = true;
    };
  }, [
    applyResumedSession,
    permissionMode,
    refreshGit,
    refreshSessions,
    selectedWorkspace,
    showToast,
    token,
  ]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy, pending]);

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
    clearPageSessionId(window.history);
    sessionRequestsRef.current.selectWorkspace(workspace.workspace_id);
    localStorage.setItem(WORKSPACE_KEY, workspace.workspace_id);
    setSessions([]);
    setSessionsLoading(true);
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

  async function openSessionsPanel() {
    setPanel("sessions");
    try {
      await refreshSessions();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function sendDuringRun(cleanPrompt) {
    const expectedTurnId = activeTurnId;
    if (!expectedTurnId) {
      showToast("尚未取得当前 Turn 标识，请稍后重试。");
      return;
    }
    const localId = createInteractionId("input");
    const localInput = createPendingInput(cleanPrompt, deliveryMode, localId);
    setPendingInputs((items) => [...items, localInput]);
    setPrompt("");
    try {
      const data = await request(token, "/api/turn/message", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          expected_turn_id: expectedTurnId,
          mode: deliveryMode,
          prompt: cleanPrompt,
        }),
      });
      const serverItem = data.queued_message;
      setPendingInputs((items) => items.map((item) => (
        item.id === localId
          ? {
              ...item,
              id: serverItem?.id || item.id,
              prompt: serverItem?.prompt || item.prompt,
              createdAt: serverItem?.created_at || "",
              status: data.accepted === false ? "rejected" : "accepted",
              detail: data.mode === "queue" ? "等待当前回答完成" : "将在下一安全点生效",
            }
          : item
      )));
    } catch (error) {
      setPendingInputs((items) => settlePendingInput(items, localId, "failed", error.message));
      setPrompt(cleanPrompt);
      showToast(error.message);
    }
  }

  function resetLiveTimeline() {
    streamTextRef.current = "";
    setStreamText("");
    setLiveWork([]);
  }

  function appendLiveToken(text) {
    streamTextRef.current += text || "";
    setStreamText(streamTextRef.current);
  }

  function acceptLiveWorkEvent(event) {
    if (event.phase === "narrative") {
      streamTextRef.current = "";
      setStreamText("");
    }
    setLiveWork((items) => mergeWorkEvent(items, event));
  }

  function beginEditTurn(turn) {
    setEditingTurnId(turn.id);
    setEditDraft(turn.user?.content || "");
  }

  async function saveEditedTurn(event, turn) {
    event.preventDefault();
    const cleanPrompt = editDraft.trim();
    if (!cleanPrompt || editBusy || busy || !selectedWorkspace) return;
    const turnId = turn.user?.turn_id || turn.id;
    const previousMessages = messages;
    setMessages((items) => {
      const targetIndex = items.findLastIndex((message) => (
        message.role === "user" && (!message.turn_id || message.turn_id === turnId)
      ));
      if (targetIndex < 0) return items;
      return items.slice(0, targetIndex + 1).map((message, index) => (
        index === targetIndex ? { ...message, content: cleanPrompt } : message
      ));
    });
    setEditingTurnId("");
    setEditBusy(true);
    setBusy(true);
    setStatus("running");
    streamTextRef.current = "";
    setStreamText("");
    setRunStage("queued");
    setLiveWork([]);
    setRunStartedAt(Date.now());
    setActiveTurnId(turnId);
    try {
      let payload = null;
      await streamRequest(token, "/api/turn/edit/stream", {
        client_id: clientId,
        workspace_id: selectedWorkspace.workspace_id,
        turn_id: turnId,
        prompt: cleanPrompt,
        permission_mode: permissionMode,
      }, (streamEvent) => {
        const startedTurnId = streamEvent.type === "turn"
          ? streamEvent.turn_id
          : streamEvent.details?.turn_id;
        if (startedTurnId) setActiveTurnId(startedTurnId);
        if (streamEvent.type === "token") {
          appendLiveToken(streamEvent.text);
        } else if (streamEvent.type === "stage") {
          setRunStage(streamEvent.stage || "");
        } else if (streamEvent.type === "work") {
          acceptLiveWorkEvent(streamEvent);
        } else if (streamEvent.type === "result") {
          payload = streamEvent.data;
        } else if (streamEvent.type === "error") {
          const streamError = new Error(streamEvent.error || "重新回答失败。");
          streamError.status = streamEvent.status_code;
          throw streamError;
        }
      });
      if (!payload) throw new Error("流式响应结束但没有最终结果。");
      const result = payload.result || payload;
      let synchronized = payload.messages || [];
      try {
        const refreshed = await refreshMessages();
        if (refreshed.length > 0) synchronized = refreshed;
      } catch (error) {
        showToast(`操作记录刷新失败：${error.message}`);
      }
      if (synchronized.length > 0) {
        setMessages(attachChangedFilesToLatestTurn(
          synchronized,
          payload.changed_files || result.changed_files || [],
        ));
      } else if (result.text) {
        setMessages((items) => [
          ...attachChangedFilesToLatestTurn(
            items,
            payload.changed_files || result.changed_files || [],
          ),
          { role: "assistant", content: result.text },
        ]);
      }
      setPending(hasPendingApprovals(result) ? result : null);
      setStatus(result.status || "completed");
      setContextUsage({
        used_tokens: result.context_used_tokens || 0,
        window_tokens: result.context_window_tokens || bootstrap.context_window_tokens || 0,
      });
      showToast("已根据编辑后的提问重新回答，工作区文件保持原状");
      refreshGit().catch((error) => showToast(`Git 刷新失败：${error.message}`));
    } catch (error) {
      setMessages(previousMessages);
      setEditingTurnId(turn.id);
      showToast(error.message);
    } finally {
      setEditBusy(false);
      setBusy(false);
      streamTextRef.current = "";
      setStreamText("");
      setRunStage("");
      setLiveWork([]);
      setRunStartedAt(0);
      setActiveTurnId("");
    }
  }

  async function changeTurnFiles(turn, action) {
    if (!selectedWorkspace) return;
    const turnId = turn.user?.turn_id || turn.id;
    setChangeActionStates((states) => ({
      ...states,
      [turn.id]: { ...states[turn.id], busy: true, busyAction: action, conflict: false, detail: "" },
    }));
    try {
      const data = await request(token, "/api/changes/action", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          turn_id: turnId,
          action,
        }),
      });
      const normalized = normalizeChangeAction(data, action);
      setChangeActionStates((states) => ({
        ...states,
        [turn.id]: {
          ...normalized,
          canUndo: data.can_undo,
          canReapply: data.can_reapply,
          detail: data.blocked_reason || normalized.detail,
          busy: false,
          busyAction: "",
        },
      }));
      if (data.git) setGitState(data.git);
      else refreshGit().catch((error) => showToast(`Git 刷新失败：${error.message}`));
    } catch (error) {
      setChangeActionStates((states) => ({
        ...states,
        [turn.id]: {
          ...states[turn.id],
          busy: false,
          busyAction: "",
          conflict: error.status === 409,
          detail: error.message,
        },
      }));
      showToast(error.message);
    }
  }

  async function submitPrompt(value = prompt) {
    const cleanPrompt = value.trim();
    if ((!cleanPrompt && attachments.length === 0) || !selectedWorkspace) return;
    if (!runnerOnline) {
      showToast("本机 Runner 离线，暂时不能执行任务。");
      return;
    }
    if (busy) {
      if (attachments.length > 0) {
        showToast("运行中的引导消息暂不支持附件，请等待当前回答结束。");
        return;
      }
      await sendDuringRun(cleanPrompt);
      return;
    }
    const pendingAttachments = attachments;
    const attachmentSummary = pendingAttachments.map(({ file }) => ({
      name: file.name,
      media_type: file.type || "application/octet-stream",
      size: file.size,
    }));
    setMessages((items) => [
      ...items,
      {
        role: "user",
        content: cleanPrompt || "请分析我上传的文件。",
        attachments: attachmentSummary,
      },
    ]);
    setPrompt("");
    setAttachments([]);
    setBusy(true);
    setStatus("running");
    streamTextRef.current = "";
    setStreamText("");
    setRunStage("queued");
    setLiveWork([]);
    setRunStartedAt(Date.now());
    setLastTimings(null);
    try {
      const encodedAttachments = await Promise.all(
        pendingAttachments.map(({ file }) => encodeAttachment(file)),
      );
      let result = null;
      await streamRequest(
        token,
        "/api/chat/stream",
        {
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          prompt: cleanPrompt,
          attachments: encodedAttachments,
          permission_mode: permissionMode,
        },
        (event) => {
          if (event.type === "turn" && event.phase === "queued_starting") {
            resetLiveTimeline();
          } else if (event.type === "token") {
            appendLiveToken(event.text);
          } else if (event.type === "stage") {
            setRunStage(event.stage || "");
          } else if (event.type === "work") {
            acceptLiveWorkEvent(event);
          } else if (event.type === "result") {
            result = event.data;
            setLastTimings(event.timings || null);
          } else if (event.type === "error") {
            const error = new Error(event.error || "本机 Runner 执行失败。");
            error.status = event.status_code;
            throw error;
          }
          if (event.turn_id || event.expected_turn_id) {
            setActiveTurnId(event.turn_id || event.expected_turn_id);
          }
          const startedTurnId = event.type === "turn"
            ? event.turn_id
            : event.details?.turn_id;
          if (startedTurnId) setActiveTurnId(startedTurnId);
        },
      );
      if (!result) throw new Error("流式响应结束但没有最终结果。");
      let synchronized = [];
      try {
        synchronized = await refreshMessages();
      } catch (error) {
        showToast(`操作记录刷新失败：${error.message}`);
      }
      if (synchronized.length > 0) {
        synchronized = attachChangedFilesToLatestTurn(
          synchronized,
          result.changed_files || [],
        );
        const lastAssistant = synchronized.findLastIndex(
          (message) => message.role === "assistant" && !(message.tool_calls?.length),
        );
        if (lastAssistant >= 0 && result.files?.length) {
          synchronized[lastAssistant] = {
            ...synchronized[lastAssistant],
            files: result.files,
          };
        }
        setMessages(synchronized);
      } else if (result.text || result.files?.length) {
        setMessages((items) => [
          ...attachChangedFilesToLatestTurn(items, result.changed_files || []),
          {
            role: "assistant",
            content: result.text || "文件已准备好。",
            files: result.files || [],
          },
        ]);
      }
      setPending(hasPendingApprovals(result) ? result : null);
      setPendingInputs([]);
      setStatus(result.status || "completed");
      setContextUsage({
        used_tokens: result.context_used_tokens || 0,
        window_tokens:
          result.context_window_tokens
          || bootstrap.context_window_tokens
          || 0,
      });
      if (result.session_id) {
        setActiveSessionId(result.session_id);
        storePageSessionId(
          window.history,
          selectedWorkspace.workspace_id,
          result.session_id,
        );
      }
      streamTextRef.current = "";
      setStreamText("");
      setRunStage("");
      setLiveWork([]);
      setRunStartedAt(0);
      setBusy(false);
      setActiveTurnId("");
      try {
        await refreshSessions();
      } catch (error) {
        showToast(`会话列表刷新失败：${error.message}`);
      }
      refreshGit().catch((error) => showToast(`Git 刷新失败：${error.message}`));
    } catch (error) {
      setMessages((items) => [
        ...items,
        { role: "assistant", content: `请求失败：${error.message}` },
      ]);
      setStatus("failed");
      if (error.status === 401) logout("访问令牌已失效。");
    } finally {
      setBusy(false);
      setActiveTurnId("");
      streamTextRef.current = "";
      setStreamText("");
      setRunStage("");
      setLiveWork([]);
      setRunStartedAt(0);
      pendingAttachments.forEach(
        (item) => item.preview && URL.revokeObjectURL(item.preview),
      );
    }
  }

  async function handleOutputFile(file, openInBrowser) {
    if (!selectedWorkspace || downloadingFileId) return;
    const previewWindow = openInBrowser ? window.open("", "_blank") : null;
    if (openInBrowser && !previewWindow) {
      showToast("浏览器阻止了新页面，请允许弹窗后重试。");
      return;
    }
    setDownloadingFileId(file.file_id);
    try {
      const response = await fetch("/api/download", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          file_id: file.file_id,
        }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        const error = new Error(data.detail || `文件获取失败 (${response.status})`);
        error.status = response.status;
        throw error;
      }
      const url = URL.createObjectURL(await response.blob());
      if (previewWindow) {
        previewWindow.location.href = url;
      } else {
        const link = document.createElement("a");
        link.href = url;
        link.download = file.name;
        document.body.appendChild(link);
        link.click();
        link.remove();
      }
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) {
      previewWindow?.close();
      showToast(error.message);
      if (error.status === 401) logout("访问令牌已失效。");
    } finally {
      setDownloadingFileId("");
    }
  }

  function addAttachments(fileList) {
    const selected = Array.from(fileList || []);
    const valid = [];
    for (const file of selected) {
      if (file.size > 10 * 1024 * 1024) {
        showToast(`${file.name} 超过 10 MB`);
        continue;
      }
      valid.push({
        file,
        preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : "",
      });
    }
    setAttachments((current) => {
      const slots = Math.max(0, 5 - current.length);
      if (valid.length > slots) showToast("每次最多上传 5 个文件");
      valid.slice(slots).forEach(
        (item) => item.preview && URL.revokeObjectURL(item.preview),
      );
      return [...current, ...valid.slice(0, slots)];
    });
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function removeAttachment(index) {
    setAttachments((current) => {
      const target = current[index];
      if (target?.preview) URL.revokeObjectURL(target.preview);
      return current.filter((_, itemIndex) => itemIndex !== index);
    });
  }

  async function continueApprovalBatch(batch) {
    setBusy(true);
    resetLiveTimeline();
    try {
      let result = null;
      await streamRequest(token, "/api/turn/continue/stream", {
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          expected_turn_id: batch.turn_id || pending.task_id,
          batch_id: batch.batch_id || pending.approval_batch_id,
        }, (event) => {
          if (event.type === "token") {
            appendLiveToken(event.text);
          } else if (event.type === "stage") {
            setRunStage(event.stage || "");
          } else if (event.type === "work") {
            acceptLiveWorkEvent(event);
          } else if (event.type === "result") {
            result = event.data;
          } else if (event.type === "error") {
            const streamError = new Error(event.error || "审批后继续执行失败。");
            streamError.status = event.status_code;
            throw streamError;
          }
      });
      if (!result) throw new Error("审批流结束但没有最终结果。");
      let synchronized = [];
      try {
        synchronized = await refreshMessages();
      } catch (error) {
        showToast(`操作记录刷新失败：${error.message}`);
      }
      if (synchronized.length > 0) {
        synchronized = attachChangedFilesToLatestTurn(
          synchronized,
          result.changed_files || [],
        );
        const lastAssistant = synchronized.findLastIndex(
          (message) => message.role === "assistant" && !(message.tool_calls?.length),
        );
        if (lastAssistant >= 0 && result.files?.length) {
          synchronized[lastAssistant] = {
            ...synchronized[lastAssistant],
            files: result.files,
          };
        }
        setMessages(synchronized);
      } else if (result.text || result.files?.length) {
        setMessages((items) => [
          ...attachChangedFilesToLatestTurn(items, result.changed_files || []),
          {
            role: "assistant",
            content: result.text || "文件已准备好。",
            files: result.files || [],
          },
        ]);
      }
      setPending(hasPendingApprovals(result) ? result : null);
      setStatus(result.status || "completed");
      setContextUsage({
        used_tokens: result.context_used_tokens || 0,
        window_tokens:
          result.context_window_tokens
          || bootstrap.context_window_tokens
          || 0,
      });
      await refreshSessions();
    } catch (error) {
      showToast(error.message);
    } finally {
      setBusy(false);
      streamTextRef.current = "";
      setStreamText("");
      setRunStage("");
      setLiveWork([]);
    }
  }

  async function resolveApproval(approval, action) {
    if (!pending || busy || !selectedWorkspace || !approval?.approval_id) return;
    setApprovalBusyIds((items) => ({ ...items, [approval.approval_id]: true }));
    try {
      const batch = await request(token, "/api/approval/decision", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          approval_id: approval.approval_id,
          expected_turn_id: pending.task_id,
          batch_id: pending.approval_batch_id,
          action,
        }),
      });
      setPending((current) => (
        current
          ? {
              ...current,
              approval_batch_id: batch.batch_id,
              pending_approvals: batch.approvals,
            }
          : current
      ));
      if (batch.ready) {
        await continueApprovalBatch(batch);
      }
    } catch (error) {
      showToast(error.message);
    } finally {
      setApprovalBusyIds((items) => {
        const next = { ...items };
        delete next[approval.approval_id];
        return next;
      });
    }
  }

  async function changePermissionMode(nextMode) {
    if (!selectedWorkspace || nextMode === permissionMode) return;
    const previous = permissionMode;
    setPermissionMode(nextMode);
    localStorage.setItem(PERMISSION_MODE_KEY, nextMode);
    try {
      await request(token, "/api/permission-mode", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          permission_mode: nextMode,
        }),
      });
      showToast(nextMode === "full_access" ? "已启用完全访问" : "已启用请求批准");
    } catch (error) {
      setPermissionMode(previous);
      localStorage.setItem(PERMISSION_MODE_KEY, previous);
      showToast(error.message);
    }
  }

  async function resumeSession(sessionId) {
    if (busy || !selectedWorkspace) return;
    setBusy(true);
    setPendingInputs([]);
    setEditingTurnId("");
    setChangeActionStates({});
    setResumingSessionId(sessionId);
    try {
      const workspaceId = selectedWorkspace.workspace_id;
      const data = await requestSessionResume(token, {
        clientId,
        workspaceId,
        sessionId,
        permissionMode,
      });
      applyResumedSession(workspaceId, sessionId, data);
      showToast("会话已恢复");
    } catch (error) {
      showToast(error.message);
    } finally {
      setResumingSessionId("");
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
    clearPageSessionId(window.history);
    setActiveSessionId("");
    setMessages([]);
    setPendingInputs([]);
    setEditingTurnId("");
    setChangeActionStates({});
    setPending(null);
    setStatus("idle");
    streamTextRef.current = "";
    setLiveWork([]);
    setRunStartedAt(0);
    setContextUsage({
      used_tokens: 0,
      window_tokens: bootstrap.context_window_tokens || 0,
    });
    setPanel(null);
    showToast("已新建会话");
  }

  async function deleteSession() {
    if (!deleteTarget || busy || !selectedWorkspace) return;
    const sessionId = deleteTarget.session_id;
    setDeletingSessionId(sessionId);
    try {
      await request(token, "/api/sessions/delete", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          session_id: sessionId,
        }),
      });
      if (activeSessionId === sessionId) {
        renewClientId(selectedWorkspace.workspace_id);
        clearPageSessionId(window.history);
        setActiveSessionId("");
        setMessages([]);
        setPending(null);
        setStatus("idle");
      }
      setDeleteTarget(null);
      await refreshSessions();
      showToast("历史会话已删除");
    } catch (error) {
      showToast(error.message);
    } finally {
      setDeletingSessionId("");
    }
  }

  async function openInfo(kind) {
    if (!selectedWorkspace) return;
    try {
      const suffix = `workspace_id=${encodeURIComponent(selectedWorkspace.workspace_id)}`;
      const path = kind === "diagnostics"
        ? `/api/diagnostics?${suffix}`
        : `/api/${kind}/${clientId}?${suffix}`;
      const data = await request(token, path);
      setPanelContent(
        typeof data.summary === "string" ? data.summary : JSON.stringify(data.summary, null, 2),
      );
      setPanel(kind);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function loadGitDiff(scope = gitView, path = "", base = "") {
    if (!selectedWorkspace || !runnerOnline) return null;
    setGitLoading(true);
    try {
      const data = await request(token, "/api/git/diff", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: selectedWorkspace.workspace_id,
          scope,
          path,
          base,
        }),
      });
      setGitDiff(data);
      return data;
    } catch (error) {
      showToast(error.message);
      return null;
    } finally {
      setGitLoading(false);
    }
  }

  async function openGit(view = "changes") {
    if (!selectedWorkspace) return;
    setGitOpen(true);
    setGitView(view);
    setGitDiff(null);
    setMobileNavOpen(false);
    try {
      const latest = await refreshGit();
      if (!latest?.available) return;
      const base = view === "compare" ? latest.default_base : "";
      await loadGitDiff(view, "", base);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function loadProjectFiles() {
    if (!selectedWorkspace || !runnerOnline) return [];
    setFilePanelLoading(true);
    try {
      const suffix = `workspace_id=${encodeURIComponent(selectedWorkspace.workspace_id)}`;
      const data = await request(token, `/api/files?${suffix}`);
      setProjectFiles(data.files || []);
      setProjectFilesTruncated(Boolean(data.truncated));
      return data.files || [];
    } catch (error) {
      showToast(error.message);
      return [];
    } finally {
      setFilePanelLoading(false);
    }
  }

  async function openFilePanel(mode = "changed", path = "") {
    if (!selectedWorkspace) return;
    setFilePanelOpen(true);
    setFilePanelMode(mode);
    setSelectedFilePath(path);
    setSelectedFile(null);
    setMobileNavOpen(false);
    if (mode === "changed") {
      setGitDiff(null);
      const latest = await refreshGit();
      const target = path || "";
      if (target && latest?.changes?.some((item) => item.path === target)) {
        await loadGitDiff("changes", target);
      }
      return;
    }
    if (!projectFiles.length) await loadProjectFiles();
  }

  async function selectProjectFile(path) {
    if (!selectedWorkspace) return;
    setSelectedFilePath(path);
    setSelectedFile(null);
    setFilePanelLoading(true);
    try {
      const data = await request(token, "/api/files/read", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: selectedWorkspace.workspace_id,
          path,
        }),
      });
      setSelectedFile(data);
    } catch (error) {
      showToast(error.message);
    } finally {
      setFilePanelLoading(false);
    }
  }

  async function changeFilePanelMode(mode) {
    setFilePanelMode(mode);
    setSelectedFilePath("");
    setSelectedFile(null);
    setGitDiff(null);
    if (mode === "all") {
      if (!projectFiles.length) await loadProjectFiles();
    }
  }

  async function handleGitView(view, base = "") {
    setGitView(view);
    setGitDiff(null);
    await loadGitDiff(view, "", view === "compare" ? base : "");
  }

  async function handleGitAction(action, values = {}) {
    if (!selectedWorkspace || gitBusy || busy) return false;
    setGitBusy(true);
    try {
      const result = await request(token, "/api/git/action", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: selectedWorkspace.workspace_id,
          action,
          paths: values.paths || [],
          branch: values.branch || "",
          message: values.message || "",
        }),
      });
      setGitState(result.git);
      const labels = {
        stage: "文件已暂存",
        unstage: "已取消暂存",
        switch: `已切换到 ${result.git.branch}`,
        create_branch: `已创建并切换到 ${result.git.branch}`,
        commit: "提交已创建",
        push: "当前分支已推送",
      };
      showToast(labels[action] || "Git 操作完成");
      if (action === "switch" || action === "create_branch") {
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
        setGitView("changes");
        await loadGitDiff("changes");
      } else if (action !== "push") {
        await loadGitDiff(
          gitView,
          action === "commit" ? "" : gitDiff?.path || "",
          gitDiff?.base || "",
        );
      }
      return true;
    } catch (error) {
      showToast(error.message);
      return false;
    } finally {
      setGitBusy(false);
    }
  }

  function launchGitReview(view, base, path) {
    if (busy) return;
    const range = view === "compare"
      ? `当前分支相对 ${base} 的变更`
      : "当前未提交变更";
    const target = path ? `，重点审查文件 ${path}` : "";
    setGitOpen(false);
    submitPrompt(
      `请以代码审查者身份 Review ${range}${target}。优先发现会导致错误、回归、数据损坏、`
      + "安全问题或缺失测试的具体问题；按严重程度列出发现，并提供文件和行号。"
      + "如果没有发现问题，请明确说明剩余测试风险。",
    );
  }

  function addDiffLineToPrompt(path, line) {
    const lineNumber = line.next || line.old;
    setPrompt(
      `请重点检查 ${path}:${lineNumber} 这一处变更，解释它是否存在缺陷或回归风险：\n`
      + line.text,
    );
    setGitOpen(false);
    window.requestAnimationFrame(() => promptInputRef.current?.focus());
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
          <button type="button" onClick={() => openInfo("diagnostics")}>
            <TerminalSquare size={18} /> 本地诊断
          </button>
          <button
            type="button"
            onClick={openSessionsPanel}
          >
            <History size={18} /> 历史会话
            {sessions.length > 0 && <em>{sessions.length}</em>}
          </button>
          <button type="button" onClick={() => openFilePanel("all")}>
            <Folder size={18} /> 文件
          </button>
        </nav>

        <nav className="side-nav git-side-nav">
          <span className="nav-caption">GIT</span>
          <button type="button" onClick={() => openFilePanel("changed")}>
            <FileDiff size={18} /> Changes
            {gitState?.available && (
              <span className="sidebar-git-stats">
                <b>+{gitState.additions}</b>
                <i>-{gitState.deletions}</i>
              </span>
            )}
          </button>
          <button type="button" onClick={() => openGit("changes")}>
            <GitBranch size={18} /> Git 管理
            <span className="branch-nav-copy">
              <small>{gitState?.available ? gitState.branch : "非 Git 项目"}</small>
            </span>
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
            <ContextMeter usage={contextUsage} />
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
                {conversationTurns.map((turn, index) => (
                  <ConversationTurn
                    key={turn.id}
                    turn={turn}
                    active={busy && index === conversationTurns.length - 1}
                    liveWork={liveWork}
                    liveText={streamText}
                    liveStartedAt={runStartedAt}
                    stage={runStage}
                    onFileAction={handleOutputFile}
                    onOpenChanges={(path) => openFilePanel("changed", path)}
                    downloadingFileId={downloadingFileId}
                    editable={turn.id === editableTurnId}
                    editing={turn.id === editingTurnId}
                    editDraft={editDraft}
                    editBusy={editBusy}
                    onEdit={() => beginEditTurn(turn)}
                    onEditDraft={setEditDraft}
                    onCancelEdit={() => setEditingTurnId("")}
                    onSaveEdit={(event) => saveEditedTurn(event, turn)}
                    actionState={changeActionStates[turn.id]}
                    onChangeAction={(action) => changeTurnFiles(turn, action)}
                  />
                ))}
                {pending && (
                  <div className="approval-batch" aria-label="待确认操作">
                    {(pending.pending_approvals?.length
                      ? pending.pending_approvals.filter((item) => item.decision === "pending")
                      : [pending]
                    ).map((approval, index, items) => (
                      <ApprovalRequest
                        key={approval.approval_id || approval.tool_call_id || "legacy-approval"}
                        pending={approval}
                        busy={busy || Boolean(approvalBusyIds[approval.approval_id])}
                        position={index + 1}
                        total={items.length}
                        onResolve={(action) => resolveApproval(approval, action)}
                      />
                    ))}
                  </div>
                )}
                <div ref={messageEndRef} />
              </div>
            )}
          </div>
        </section>

        <footer className="composer-area">
          <PendingInputs items={pendingInputs} />
          <div className="composer">
            {attachments.length > 0 && (
              <div className="attachment-strip">
                {attachments.map((item, index) => (
                  <span className="attachment-chip" key={`${item.file.name}-${item.file.lastModified}`}>
                    {item.preview ? (
                      <img src={item.preview} alt="" />
                    ) : (
                      <FileText size={16} />
                    )}
                    <b>{item.file.name}</b>
                    <button
                      type="button"
                      onClick={() => removeAttachment(index)}
                      aria-label={`移除 ${item.file.name}`}
                    >
                      <X size={13} />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <textarea
              ref={promptInputRef}
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
                  ? busy
                    ? deliveryMode === "steer"
                      ? "发送引导，下一安全点会交给当前回答…"
                      : "排到下一轮，当前回答完成后自动发送…"
                    : `在 ${selectedWorkspace.name} 中告诉 AutoCode 你想完成什么…`
                  : "请先选择项目"
              }
              disabled={!selectedWorkspace || !runnerOnline}
            />
            <div className="composer-bottom">
              {busy ? (
                <div className="delivery-mode" role="group" aria-label="运行中消息发送方式">
                  <button
                    type="button"
                    className={deliveryMode === "steer" ? "active" : ""}
                    aria-pressed={deliveryMode === "steer"}
                    onClick={() => setDeliveryMode("steer")}
                  >引导当前</button>
                  <button
                    type="button"
                    className={deliveryMode === "queue" ? "active" : ""}
                    aria-pressed={deliveryMode === "queue"}
                    onClick={() => setDeliveryMode("queue")}
                  >下一轮</button>
                </div>
              ) : (
                <label className="permission-mode">
                  <ShieldCheck size={14} />
                  <select
                    value={permissionMode}
                    onChange={(event) => changePermissionMode(event.target.value)}
                    aria-label="工具权限"
                  >
                    <option value="ask">请求批准</option>
                    <option value="full_access">完全访问</option>
                  </select>
                </label>
              )}
              <div className="composer-actions">
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  hidden
                  onChange={(event) => addAttachments(event.target.files)}
                />
                <button
                  className="attach-button"
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={busy || !runnerOnline || !selectedWorkspace}
                  aria-label="上传文件或图片"
                  title="上传文件或图片"
                >
                  <Paperclip size={18} />
                </button>
                <button
                  type="button"
                  onClick={() => submitPrompt()}
                  disabled={
                    (!prompt.trim() && attachments.length === 0)
                    || (busy && !activeTurnId)
                    || !runnerOnline
                    || !selectedWorkspace
                  }
                  aria-label="发送"
                >
                  <Send size={18} />
                </button>
              </div>
            </div>
          </div>
          <p>{busy ? "Enter 发送 · 可选择引导当前回答或排到下一轮" : "Enter 发送 · Shift + Enter 换行 · 危险操作会等待确认"}</p>
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

      <FilePanel
        open={filePanelOpen}
        mode={filePanelMode}
        changedFiles={gitState?.changes || []}
        files={projectFiles}
        selectedPath={selectedFilePath}
        diff={gitDiff}
        file={selectedFile}
        loading={filePanelLoading || gitLoading}
        truncated={projectFilesTruncated}
        onClose={() => setFilePanelOpen(false)}
        onModeChange={changeFilePanelMode}
        onSelectChanged={async (path) => {
          setSelectedFilePath(path);
          await loadGitDiff("changes", path);
        }}
        onSelectFile={selectProjectFile}
        onOpenGit={() => {
          setFilePanelOpen(false);
          openGit("changes");
        }}
      />

      <GitPanel
        open={gitOpen}
        state={gitState}
        diff={gitDiff}
        view={gitView}
        loading={gitLoading}
        busy={gitBusy || busy}
        onClose={() => setGitOpen(false)}
        onRefresh={async () => {
          const latest = await refreshGit();
          if (latest?.available) {
            await loadGitDiff(
              gitView,
              "",
              gitView === "compare"
                ? gitDiff?.base || latest.default_base
                : "",
            );
          }
        }}
        onChangeView={handleGitView}
        onLoadDiff={loadGitDiff}
        onAction={handleGitAction}
        onReview={launchGitReview}
        onLineFeedback={addDiffLineToPrompt}
      />

      {panel && (
        <div className="modal-layer" role="dialog" aria-modal="true">
          <section className="info-panel">
            <header>
              <div>
                <span className="overline">
                  {panel === "sessions"
                    ? "SESSION HISTORY"
                    : panel === "diagnostics"
                      ? "LOCAL DIAGNOSTICS"
                      : "RUNTIME DETAILS"}
                </span>
                <h2>
                  {panel === "sessions"
                    ? "历史会话"
                    : panel === "trace"
                      ? "运行 Trace"
                      : panel === "diagnostics"
                        ? "本地诊断"
                        : "当前任务"}
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
                  {sessionsLoading ? (
                    <div className="panel-empty session-loading" role="status">
                      <RefreshCw className="spin" size={17} />
                      正在加载当前项目的历史会话…
                    </div>
                  ) : sessions.map((session) => (
                    <div
                      key={session.session_id}
                      className={`session-row ${
                        activeSessionId === session.session_id ? "is-active" : ""
                      }`}
                    >
                      <button
                        type="button"
                        className={resumingSessionId === session.session_id ? "session-open is-resuming" : "session-open"}
                        disabled={busy || deletingSessionId === session.session_id}
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
                        {resumingSessionId === session.session_id
                          ? <RefreshCw className="spin" size={16} />
                          : <ArrowRight size={16} />}
                      </button>
                      <button
                        type="button"
                        className="session-delete"
                        aria-label={`删除会话 ${session.title || session.session_id}`}
                        disabled={busy || Boolean(deletingSessionId)}
                        onClick={() => setDeleteTarget(session)}
                      >
                        {deletingSessionId === session.session_id
                          ? <RefreshCw className="spin" size={15} />
                          : <Trash2 size={15} />}
                      </button>
                    </div>
                  ))}
                  {!sessionsLoading && !sessions.length && (
                    <div className="panel-empty">这个项目还没有可恢复的会话。</div>
                  )}
                </div>
              </div>
            ) : (
              <pre className="runtime-content">{panelContent}</pre>
            )}
          </section>
        </div>
      )}

      {deleteTarget && (
        <div className="modal-layer confirm-layer" role="dialog" aria-modal="true" aria-labelledby="delete-session-title">
          <section className="confirm-dialog">
            <span className="confirm-icon"><Trash2 size={21} /></span>
            <h2 id="delete-session-title">删除这段历史会话？</h2>
            <p>
              “{deleteTarget.title || deleteTarget.session_id}” 的聊天记录将从本机永久删除，
              此操作无法撤销。
            </p>
            <div className="confirm-actions">
              <button type="button" disabled={Boolean(deletingSessionId)} onClick={() => setDeleteTarget(null)}>
                取消
              </button>
              <button className="danger-button" type="button" disabled={Boolean(deletingSessionId)} onClick={deleteSession}>
                {deletingSessionId ? "正在删除…" : "确认删除"}
              </button>
            </div>
          </section>
        </div>
      )}

      {toast && <div className="toast"><Check size={16} /> {toast}</div>}
    </div>
  );
}
