import {
  FileText,
  Paperclip,
  Send,
  ShieldCheck,
  X,
} from "lucide-react";

import { PendingInputs } from "./ConversationView";

export default function Composer({
  selectedWorkspace,
  runnerOnline,
  busy,
  activeTurnId,
  prompt,
  attachments,
  pendingInputs,
  deliveryMode,
  permissionMode,
  fileInputRef,
  promptInputRef,
  onPromptChange,
  onSubmit,
  onDeliveryModeChange,
  onPermissionModeChange,
  onAddAttachments,
  onRemoveAttachment,
}) {
  return (
    <footer className="composer-area">
      <PendingInputs items={pendingInputs} />
      <div className="composer">
        {attachments.length > 0 && (
          <div className="attachment-strip">
            {attachments.map((item, index) => (
              <span
                className="attachment-chip"
                key={`${item.file.name}-${item.file.lastModified}`}
              >
                {item.preview
                  ? <img src={item.preview} alt="" />
                  : <FileText size={16} />}
                <b>{item.file.name}</b>
                <button
                  type="button"
                  onClick={() => onRemoveAttachment(index)}
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
          onChange={(event) => onPromptChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSubmit();
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
                onClick={() => onDeliveryModeChange("steer")}
              >引导当前</button>
              <button
                type="button"
                className={deliveryMode === "queue" ? "active" : ""}
                aria-pressed={deliveryMode === "queue"}
                onClick={() => onDeliveryModeChange("queue")}
              >下一轮</button>
            </div>
          ) : (
            <label className="permission-mode">
              <ShieldCheck size={14} />
              <select
                value={permissionMode}
                onChange={(event) => onPermissionModeChange(event.target.value)}
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
              onChange={(event) => {
                onAddAttachments(event.target.files);
                event.currentTarget.value = "";
              }}
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
              onClick={onSubmit}
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
      <p>
        {busy
          ? "Enter 发送 · 可选择引导当前回答或排到下一轮"
          : "Enter 发送 · Shift + Enter 换行 · 危险操作会等待确认"}
      </p>
    </footer>
  );
}
