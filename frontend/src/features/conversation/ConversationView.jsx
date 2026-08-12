import {
  Activity,
  ArrowRight,
  ChevronDown,
  Download,
  ExternalLink,
  FileCode2,
  FileText,
  Github,
  Image as ImageIcon,
  LayoutGrid,
  Pencil,
  Redo2,
  RefreshCw,
  Send,
  Sparkles,
  TerminalSquare,
  Undo2,
} from "lucide-react";
import { useEffect, useState } from "react";

import {
  formatDuration,
  formatToolTitle,
} from "../../conversation";
import RichText from "../../markdown";

function formatFileSize(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function MessageAttachments({ attachments = [] }) {
  if (attachments.length === 0) return null;
  return (
    <div className="message-attachments">
      {attachments.map((attachment) => (
        <span key={`${attachment.name}-${attachment.size}`}>
          {attachment.media_type.startsWith("image/")
            ? <ImageIcon size={14} />
            : <FileText size={14} />}
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
            {file.media_type.startsWith("image/")
              ? <ImageIcon size={18} />
              : <FileText size={18} />}
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
              {downloadingFileId === file.file_id
                ? <RefreshCw className="spin" size={15} />
                : <Download size={15} />}
              下载
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}

function UserMessage({
  message,
  editable,
  editing,
  editDraft,
  editBusy,
  onEdit,
  onEditDraft,
  onCancelEdit,
  onSaveEdit,
}) {
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
              <button
                className="primary-edit-action"
                type="submit"
                disabled={!editDraft.trim() || editBusy}
              >
                {editBusy
                  ? <RefreshCw className="spin" size={14} />
                  : <Send size={14} />}
                重新回答
              </button>
            </div>
          </form>
        ) : (
          <>
            <RichText content={message.content} />
            <MessageAttachments attachments={message.attachments} />
            {editable && (
              <button
                className="turn-edit-button"
                type="button"
                onClick={onEdit}
                aria-label="编辑上一次提问"
                title="编辑并重新回答"
              >
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
  const [elapsedMs, setElapsedMs] = useState(
    () => Math.max(0, Date.now() - startedAt),
  );

  useEffect(() => {
    const updateElapsed = () => setElapsedMs(Math.max(0, Date.now() - startedAt));
    updateElapsed();
    const timer = window.setInterval(updateElapsed, 250);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  return <span>{`Working for ${formatDuration(elapsedMs)}`}</span>;
}

export function WorkBlock({
  items,
  elapsedMs,
  active = false,
  liveText = "",
  stage = "",
  startedAt = 0,
}) {
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
  const workLabel = elapsedMs > 0
    ? `Worked for ${formatDuration(elapsedMs)}`
    : "Worked";
  return (
    <details className={`work-block ${active ? "is-active" : ""}`} open={active}>
      <summary>
        {active ? <LiveElapsed startedAt={startedAt} /> : <span>{workLabel}</span>}
        {active && <i className="work-pulse" />}
        <ChevronDown size={16} />
      </summary>
      <div className="work-content">
        {items.map((item) => (
          item.type === "guidance" ? (
            <div className="work-guidance" key={item.id}>
              <span>引导当前</span>
              <RichText content={item.content} />
            </div>
          ) : item.type === "narrative" ? (
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

function TurnChangedFiles({
  files = [],
  actionState,
  onOpenChanges,
  onChangeAction,
}) {
  const [expanded, setExpanded] = useState(true);
  if (files.length === 0) return null;
  const persistedState = {
    state: files[0]?.state || "",
    canUndo: files[0]?.can_undo,
    canReapply: files[0]?.can_reapply,
    detail: files[0]?.blocked_reason || "",
  };
  const effectiveState = actionState || persistedState;
  const additions = files.reduce(
    (total, file) => total + Number(file.additions || 0),
    0,
  );
  const deletions = files.reduce(
    (total, file) => total + Number(file.deletions || 0),
    0,
  );
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
          <button
            type="button"
            onClick={() => onOpenChanges(file.path)}
            key={file.path}
          >
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
            disabled={
              effectiveState.busy
              || effectiveState.canUndo === false
              || effectiveState.state === "undone"
            }
          >
            {effectiveState.busyAction === "undo"
              ? <RefreshCw className="spin" size={14} />
              : <Undo2 size={14} />}
            Undo
          </button>
          <button
            type="button"
            onClick={() => onChangeAction("reapply")}
            disabled={
              effectiveState.busy
              || effectiveState.canReapply === false
              || effectiveState.state !== "undone"
            }
          >
            {effectiveState.busyAction === "reapply"
              ? <RefreshCw className="spin" size={14} />
              : <Redo2 size={14} />}
            Reapply
          </button>
          <span
            className={`change-action-state ${effectiveState.conflict ? "is-conflict" : ""}`}
            aria-live="polite"
          >
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

export function ConversationTurn({
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

export function Welcome({ workspace, onPrompt }) {
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
      <div className="welcome-symbol"><Sparkles size={27} /></div>
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

export function PendingInputs({ items }) {
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
