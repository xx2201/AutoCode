import {
  ArrowRight,
  Folder,
  FolderGit2,
  RefreshCw,
  Search,
  ShieldCheck,
  TerminalSquare,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

export default function ProjectPicker({
  open,
  workspaces,
  onSelect,
  onClose,
  onRefresh,
  required,
}) {
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
        workspace.name.toLocaleLowerCase().includes(keyword)
        || workspace.path.toLocaleLowerCase().includes(keyword),
    );
  }, [query, workspaces]);

  if (!open) return null;

  return (
    <div className="modal-layer project-layer" role="dialog" aria-modal="true">
      <section className="project-picker">
        <div className="project-picker-head">
          <div className="project-picker-mark"><FolderGit2 size={24} /></div>
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
