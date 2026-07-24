import {
  Check,
  ChevronRight,
  FileDiff,
  GitBranch,
  GitCommitHorizontal,
  GitCompare,
  Github,
  Minus,
  Plus,
  RefreshCw,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";

const STATUS_LABELS = {
  added: "新增",
  conflict: "冲突",
  copied: "复制",
  deleted: "删除",
  modified: "修改",
  renamed: "重命名",
  type_changed: "类型",
  untracked: "未跟踪",
};

function parseDiff(raw) {
  const result = [];
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  for (const source of (raw || "").split("\n").slice(0, 5000)) {
    if (source.startsWith("@@")) {
      const match = source.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
      }
      inHunk = true;
      result.push({ kind: "hunk", old: "", next: "", text: source });
    } else if (inHunk && source.startsWith("+") && !source.startsWith("+++")) {
      result.push({ kind: "added", old: "", next: newLine, text: source });
      newLine += 1;
    } else if (inHunk && source.startsWith("-") && !source.startsWith("---")) {
      result.push({ kind: "deleted", old: oldLine, next: "", text: source });
      oldLine += 1;
    } else if (inHunk && source.startsWith(" ")) {
      result.push({ kind: "context", old: oldLine, next: newLine, text: source });
      oldLine += 1;
      newLine += 1;
    } else {
      result.push({ kind: "meta", old: "", next: "", text: source });
    }
  }
  return result;
}

function ChangeStats({ additions = 0, deletions = 0 }) {
  return (
    <span className="git-stats">
      <b>+{additions}</b>
      <i>-{deletions}</i>
    </span>
  );
}

function FileList({ files, selectedPath, busy, onSelect, onAction, compare }) {
  if (!files?.length) {
    return <div className="git-empty">这个范围内没有代码变更。</div>;
  }
  return (
    <div className="git-file-list">
      {files.map((file) => (
        <div
          className={selectedPath === file.path ? "selected" : ""}
          key={`${file.old_path || ""}-${file.path}`}
        >
          <button
            className="git-file-open"
            type="button"
            onClick={() => onSelect(file.path)}
          >
            <span className={`git-status git-status-${file.status}`}>
              {(STATUS_LABELS[file.status] || file.status).slice(0, 1)}
            </span>
            <span className="git-file-copy">
              <strong title={file.path}>{file.path}</strong>
              <small>
                {STATUS_LABELS[file.status] || file.status}
                {file.staged && " · 已暂存"}
                {file.unstaged && file.staged && " · 仍有未暂存内容"}
              </small>
            </span>
            <ChangeStats additions={file.additions} deletions={file.deletions} />
            <ChevronRight size={15} />
          </button>
          {!compare && (
            <button
              className="git-file-action"
              type="button"
              title={file.staged && !file.unstaged ? "取消暂存" : "暂存文件"}
              onClick={() => {
                onAction(
                  file.staged && !file.unstaged ? "unstage" : "stage",
                  { paths: [file.path] },
                );
              }}
            >
              {busy ? (
                <RefreshCw className="spin" size={14} />
              ) : file.staged && !file.unstaged ? (
                <Minus size={14} />
              ) : (
                <Plus size={14} />
              )}
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function DiffViewer({ diff, busy, onReview, onLineFeedback }) {
  const lines = useMemo(() => parseDiff(diff?.diff), [diff?.diff]);
  const selectedFile = diff?.path || (diff?.files?.length === 1 ? diff.files[0].path : "");
  return (
    <section className="diff-viewer">
      <header>
        <div>
          <span>DIFF REVIEW</span>
          <strong>{selectedFile || "全部变更"}</strong>
        </div>
        <button
          className="review-action"
          type="button"
          onClick={() => onReview(selectedFile)}
          disabled={busy || !diff?.diff}
        >
          <Sparkles size={15} />
          让 Agent Review
        </button>
      </header>
      <div className="diff-code">
        {!lines.length || !diff?.diff ? (
          <div className="git-empty">选择文件后在这里查看逐行 Diff。</div>
        ) : (
          lines.map((line, index) => (
            <button
              type="button"
              className={`diff-line ${line.kind}`}
              key={`${index}-${line.old}-${line.next}`}
              title={
                line.kind === "added" || line.kind === "deleted"
                  ? "点击把这一行带入对话"
                  : ""
              }
              onClick={() => {
                if (line.kind === "added" || line.kind === "deleted") {
                  onLineFeedback(selectedFile, line);
                }
              }}
            >
              <span>{line.old}</span>
              <span>{line.next}</span>
              <code>{line.text || " "}</code>
            </button>
          ))
        )}
      </div>
      {diff?.truncated && <footer>Diff 过大，当前仅显示前 2 MB。</footer>}
    </section>
  );
}

export default function GitPanel({
  open,
  state,
  diff,
  view,
  loading,
  busy,
  onClose,
  onRefresh,
  onChangeView,
  onLoadDiff,
  onAction,
  onReview,
  onLineFeedback,
}) {
  const [base, setBase] = useState("");
  const [branchChoice, setBranchChoice] = useState("");
  const [newBranch, setNewBranch] = useState("");
  const [commitMessage, setCommitMessage] = useState("");
  if (!open) return null;

  const effectiveBase = base || state?.default_base || "";
  const files = view === "compare" ? diff?.files || [] : state?.changes || [];
  const stagedFiles = (state?.changes || []).filter((file) => file.staged);
  const unstagedFiles = (state?.changes || []).filter(
    (file) => file.unstaged || file.status === "untracked",
  );
  const branchNames = state?.branches?.map((item) => item.name) || [];

  return (
    <div className="git-layer" role="dialog" aria-modal="true" aria-label="Git 与代码 Review">
      <section className="git-panel">
        <header className="git-panel-head">
          <div className="git-panel-title">
            <span className="git-panel-mark"><GitBranch size={21} /></span>
            <div>
              <span className="overline">LOCAL GIT WORKSPACE</span>
              <h2>代码变更与 Review</h2>
              <p>{state?.repo_root || "Git 命令在本机 workspace 中执行"}</p>
            </div>
          </div>
          <div className="git-head-actions">
            <button type="button" onClick={onRefresh} disabled={loading || busy}>
              <RefreshCw className={loading ? "spin" : ""} size={17} />
              刷新
            </button>
            <button className="icon-button" type="button" onClick={onClose}>
              <X size={20} />
            </button>
          </div>
        </header>

        {!state?.available ? (
          <div className="git-unavailable">
            <FileDiff size={34} />
            <strong>当前项目没有可用的 Git 仓库</strong>
            <p>{state?.message || "请确认 CLI 注册的是 Git 仓库根目录。"}</p>
          </div>
        ) : (
          <>
            <section className="git-environment">
              <div className="git-current-branch">
                <GitBranch size={17} />
                <span>
                  <small>当前分支</small>
                  <strong>{state.branch}</strong>
                </span>
                {state.upstream && <em>{state.ahead} ahead · {state.behind} behind</em>}
              </div>
              <label>
                <span>切换分支</span>
                <select
                  value={branchChoice || state.branch}
                  onChange={(event) => setBranchChoice(event.target.value)}
                >
                  {branchNames.map((branch) => (
                    <option key={branch} value={branch}>{branch}</option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={busy || !branchChoice || branchChoice === state.branch}
                  onClick={() => onAction("switch", { branch: branchChoice })}
                >
                  切换
                </button>
              </label>
              <label>
                <span>新建分支</span>
                <input
                  value={newBranch}
                  onChange={(event) => setNewBranch(event.target.value)}
                  placeholder="feat/my-change"
                  maxLength={200}
                />
                <button
                  type="button"
                  disabled={busy || !newBranch.trim()}
                  onClick={async () => {
                    await onAction("create_branch", { branch: newBranch.trim() });
                    setNewBranch("");
                  }}
                >
                  创建并切换
                </button>
              </label>
            </section>

            <nav className="git-tabs">
              <button
                className={view === "changes" ? "active" : ""}
                type="button"
                onClick={() => onChangeView("changes")}
              >
                <FileDiff size={16} />
                本地 Changes
                <em>{state.changes.length}</em>
                <ChangeStats additions={state.additions} deletions={state.deletions} />
              </button>
              <button
                className={view === "compare" ? "active" : ""}
                type="button"
                onClick={() => onChangeView("compare", effectiveBase)}
              >
                <GitCompare size={16} />
                Compare branch
              </button>
              <span className={state.gh_available ? "gh-ready" : ""}>
                <Github size={15} />
                {!state.gh_available
                  ? "GitHub CLI 未安装"
                  : "GitHub CLI 可用"}
              </span>
            </nav>

            {view === "compare" && (
              <section className="compare-toolbar">
                <label>
                  基准分支
                  <select
                    value={effectiveBase}
                    onChange={(event) => setBase(event.target.value)}
                  >
                    {[...(state.branches || []), ...(state.remote_branches || [])]
                      .filter((item) => item.name !== state.branch)
                      .map((item) => (
                        <option key={item.ref} value={item.name}>{item.name}</option>
                      ))}
                  </select>
                </label>
                <button
                  type="button"
                  disabled={!effectiveBase || loading}
                  onClick={() => onLoadDiff("compare", "", effectiveBase)}
                >
                  <GitCompare size={15} />
                  与 {effectiveBase || "分支"} 比较
                </button>
              </section>
            )}

            <main className="git-review-grid">
              <aside className="git-files">
                <header>
                  <div>
                    <strong>{view === "compare" ? "分支差异" : "工作区变更"}</strong>
                    <small>{files.length} 个文件</small>
                  </div>
                  {view === "changes" && (
                    <div>
                      <button
                        type="button"
                        title="暂存全部未暂存文件"
                        disabled={busy || !unstagedFiles.length || unstagedFiles.length > 200}
                        onClick={() => onAction("stage", {
                          paths: unstagedFiles.map((file) => file.path),
                        })}
                      >
                        <Plus size={14} /> 全部暂存
                      </button>
                      <button
                        type="button"
                        title="取消暂存全部文件"
                        disabled={busy || !stagedFiles.length || stagedFiles.length > 200}
                        onClick={() => onAction("unstage", {
                          paths: stagedFiles.map((file) => file.path),
                        })}
                      >
                        <Minus size={14} /> 全部取消
                      </button>
                    </div>
                  )}
                </header>
                <FileList
                  files={files}
                  selectedPath={diff?.path || ""}
                  busy={busy}
                  compare={view === "compare"}
                  onSelect={(path) => onLoadDiff(view, path, effectiveBase)}
                  onAction={onAction}
                />
              </aside>
              <DiffViewer
                diff={diff}
                busy={busy}
                onReview={(path) => onReview(view, effectiveBase, path)}
                onLineFeedback={onLineFeedback}
              />
            </main>

            <footer className="git-commit-bar">
              <div className="git-commit-icon"><GitCommitHorizontal size={19} /></div>
              <label>
                <span>Commit message</span>
                <input
                  value={commitMessage}
                  onChange={(event) => setCommitMessage(event.target.value)}
                  placeholder={
                    stagedFiles.length
                      ? `提交 ${stagedFiles.length} 个已暂存文件`
                      : "请先暂存需要提交的文件"
                  }
                  maxLength={500}
                />
              </label>
              <button
                type="button"
                disabled={busy || !stagedFiles.length || !commitMessage.trim()}
                onClick={async () => {
                  await onAction("commit", { message: commitMessage.trim() });
                  setCommitMessage("");
                }}
              >
                <Check size={15} />
                Commit
              </button>
              <button
                className="push-action"
                type="button"
                disabled={busy || state.detached}
                onClick={() => onAction("push")}
              >
                <Upload size={15} />
                Push
              </button>
            </footer>
          </>
        )}
      </section>
    </div>
  );
}
