import { Folder, FolderGit2, RefreshCw } from "lucide-react";

import ApprovalRequest from "../approvals/ApprovalRequest";
import { ConversationTurn, Welcome } from "./ConversationView";

export default function ConversationPane({
  selectedWorkspace,
  sessionRestoring,
  turns,
  messages,
  busy,
  liveWork,
  streamText,
  runStartedAt,
  runStage,
  pending,
  approvalBusyIds,
  messageEndRef,
  downloadingFileId,
  editableTurnId,
  editingTurnId,
  editDraft,
  editBusy,
  changeActionStates,
  onOpenProjects,
  onSubmit,
  onFileAction,
  onOpenChanges,
  onBeginEdit,
  onEditDraft,
  onCancelEdit,
  onSaveEdit,
  onChangeAction,
  onResolveApproval,
}) {
  return (
    <section className="conversation">
      <div className="conversation-inner">
        {sessionRestoring ? (
          <div className="session-restore-screen" role="status">
            <RefreshCw className="spin" size={24} />
            <span className="overline">SESSION HISTORY</span>
            <h1>正在恢复历史会话</h1>
            <p>正在加载消息、工具记录和任务状态…</p>
          </div>
        ) : !selectedWorkspace ? (
          <div className="select-project-empty">
            <FolderGit2 size={36} />
            <h1>先打开一个本机项目</h1>
            <p>CoreCoder 是 Agent 引擎，真正的 workspace 由你选择。</p>
            <button className="primary-action" type="button" onClick={onOpenProjects}>
              <Folder size={18} /> 选择项目
            </button>
          </div>
        ) : messages.length === 0 ? (
          <Welcome workspace={selectedWorkspace} onPrompt={onSubmit} />
        ) : (
          <div className="message-list">
            {turns.map((turn, index) => (
              <ConversationTurn
                key={turn.id}
                turn={turn}
                active={busy && index === turns.length - 1}
                liveWork={liveWork}
                liveText={streamText}
                liveStartedAt={runStartedAt}
                stage={runStage}
                onFileAction={onFileAction}
                onOpenChanges={onOpenChanges}
                downloadingFileId={downloadingFileId}
                editable={turn.id === editableTurnId}
                editing={turn.id === editingTurnId}
                editDraft={editDraft}
                editBusy={editBusy}
                onEdit={() => onBeginEdit(turn)}
                onEditDraft={onEditDraft}
                onCancelEdit={onCancelEdit}
                onSaveEdit={(event) => onSaveEdit(event, turn)}
                actionState={changeActionStates[turn.id]}
                onChangeAction={(action) => onChangeAction(turn, action)}
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
                    onResolve={(action) => onResolveApproval(approval, action)}
                  />
                ))}
              </div>
            )}
            <div ref={messageEndRef} />
          </div>
        )}
      </div>
    </section>
  );
}
