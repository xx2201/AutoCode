import {
  ArrowRight,
  Clock3,
  MessageSquareText,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";

export default function SessionPanel({
  open,
  sessions,
  loading,
  busy,
  activeSessionId,
  resumingSessionId,
  deletingSessionId,
  onClose,
  onNewSession,
  onResume,
  onDeleteRequest,
}) {
  if (!open) return null;
  return (
    <div className="modal-layer" role="dialog" aria-modal="true">
      <section className="info-panel">
        <header>
          <div>
            <span className="overline">SESSION HISTORY</span>
            <h2>历史会话</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose}>
            <X size={20} />
          </button>
        </header>
        <div className="session-content">
          <button className="new-session" type="button" onClick={onNewSession}>
            <Plus size={18} /> 新建会话
          </button>
          <div className="session-list">
            {loading ? (
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
                  className={
                    resumingSessionId === session.session_id
                      ? "session-open is-resuming"
                      : "session-open"
                  }
                  disabled={busy || deletingSessionId === session.session_id}
                  onClick={() => onResume(session.session_id)}
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
                  onClick={() => onDeleteRequest(session)}
                >
                  {deletingSessionId === session.session_id
                    ? <RefreshCw className="spin" size={15} />
                    : <Trash2 size={15} />}
                </button>
              </div>
            ))}
            {!loading && !sessions.length && (
              <div className="panel-empty">这个项目还没有可恢复的会话。</div>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

export function DeleteSessionDialog({
  target,
  deletingSessionId,
  onCancel,
  onConfirm,
}) {
  if (!target) return null;
  return (
    <div
      className="modal-layer confirm-layer"
      role="dialog"
      aria-modal="true"
      aria-labelledby="delete-session-title"
    >
      <section className="confirm-dialog">
        <span className="confirm-icon"><Trash2 size={21} /></span>
        <h2 id="delete-session-title">删除这段历史会话？</h2>
        <p>
          “{target.title || target.session_id}” 的聊天记录将从本机永久删除，
          此操作无法撤销。
        </p>
        <div className="confirm-actions">
          <button
            type="button"
            disabled={Boolean(deletingSessionId)}
            onClick={onCancel}
          >
            取消
          </button>
          <button
            className="danger-button"
            type="button"
            disabled={Boolean(deletingSessionId)}
            onClick={onConfirm}
          >
            {deletingSessionId ? "正在删除…" : "确认删除"}
          </button>
        </div>
      </section>
    </div>
  );
}
