import { ChevronDown, ShieldCheck } from "lucide-react";

import { approvalPresentation } from "../../approval";

export function hasPendingApprovals(result) {
  return Boolean(
    result?.pending_tool
    || result?.pending_approvals?.some((item) => item.decision === "pending"),
  );
}

export default function ApprovalRequest({
  pending,
  busy,
  position,
  total,
  onResolve,
}) {
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
