import {
  ChevronDown,
  ChevronRight,
  FileCode2,
  Folder,
  FolderOpen,
  Search,
  Settings2,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { parseDiff } from "./diff";

function buildTree(paths) {
  const root = { folders: new Map(), files: [] };
  for (const path of paths) {
    const parts = path.split("/");
    let node = root;
    parts.slice(0, -1).forEach((part) => {
      if (!node.folders.has(part)) {
        node.folders.set(part, { folders: new Map(), files: [] });
      }
      node = node.folders.get(part);
    });
    node.files.push({ name: parts.at(-1), path });
  }
  return root;
}

function TreeFolder({ name, node, depth, onSelect }) {
  const [open, setOpen] = useState(depth === 0);
  return (
    <div className="file-tree-folder">
      <button
        type="button"
        className="file-tree-row"
        style={{ paddingLeft: `${12 + depth * 16}px` }}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        {open ? <FolderOpen size={17} /> : <Folder size={17} />}
        <span>{name}</span>
      </button>
      {open && (
        <>
          {[...node.folders.entries()].map(([childName, child]) => (
            <TreeFolder
              key={childName}
              name={childName}
              node={child}
              depth={depth + 1}
              onSelect={onSelect}
            />
          ))}
          {node.files.map((file) => (
            <button
              type="button"
              className="file-tree-row file-tree-file"
              style={{ paddingLeft: `${43 + depth * 16}px` }}
              onClick={() => onSelect(file.path)}
              key={file.path}
            >
              <FileCode2 size={16} />
              <span>{file.name}</span>
            </button>
          ))}
        </>
      )}
    </div>
  );
}

function DiffContent({ diff }) {
  const lines = useMemo(() => parseDiff(diff?.diff), [diff?.diff]);
  if (!diff?.diff) {
    return <div className="file-panel-empty">选择一个已修改文件查看内容。</div>;
  }
  return (
    <div className="file-diff-code">
      {lines.map((line, index) => (
        <div className={`file-diff-line ${line.kind}`} key={`${index}-${line.old}-${line.next}`}>
          <span>{line.old}</span>
          <span>{line.next}</span>
          <code>{line.text || " "}</code>
        </div>
      ))}
      {diff.truncated && <p>Diff 过大，当前仅显示前 2 MB。</p>}
    </div>
  );
}

export default function FilePanel({
  open,
  mode,
  changedFiles,
  files,
  selectedPath,
  diff,
  file,
  loading,
  truncated,
  onClose,
  onModeChange,
  onSelectChanged,
  onSelectFile,
  onOpenGit,
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    return term ? files.filter((path) => path.toLowerCase().includes(term)) : files;
  }, [files, query]);
  const tree = useMemo(() => buildTree(filtered), [filtered]);
  if (!open) return null;

  const title = mode === "changed"
    ? `已更改 ${changedFiles.length} 个文件`
    : selectedPath || "文件";

  return (
    <div className="file-panel-layer" role="dialog" aria-modal="true" aria-label="项目文件">
      <button className="file-panel-backdrop" type="button" aria-label="关闭" onClick={onClose} />
      <section className="file-panel">
        <header>
          <button type="button" onClick={onClose} aria-label="关闭"><X size={21} /></button>
          <strong title={title}>{title}</strong>
          <button type="button" onClick={onOpenGit} aria-label="打开 Git 管理" title="Git 管理">
            <Settings2 size={19} />
          </button>
        </header>
        <nav>
          <button
            className={mode === "changed" ? "active" : ""}
            type="button"
            onClick={() => onModeChange("changed")}
          >
            已修改
          </button>
          <button
            className={mode === "all" ? "active" : ""}
            type="button"
            onClick={() => onModeChange("all")}
          >
            所有文件
          </button>
        </nav>
        {mode === "changed" ? (
          <div className={`file-panel-body ${selectedPath ? "selected" : ""}`}>
            <aside className="changed-file-list">
              {changedFiles.map((item) => (
                <button
                  className={selectedPath === item.path ? "active" : ""}
                  type="button"
                  onClick={() => onSelectChanged(item.path)}
                  key={item.path}
                >
                  <FileCode2 size={16} />
                  <span>{item.path}</span>
                  <b>+{item.additions || 0}</b>
                  <i>−{item.deletions || 0}</i>
                </button>
              ))}
              {!changedFiles.length && <div className="file-panel-empty">当前没有未提交修改。</div>}
            </aside>
            <main>{loading ? <div className="file-panel-empty">正在读取…</div> : <DiffContent diff={diff} />}</main>
          </div>
        ) : (
          <div className={`file-panel-body all-files ${file ? "selected" : ""}`}>
            <aside>
              <label className="file-search">
                <Search size={16} />
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="搜索文件"
                />
              </label>
              <div className="file-tree">
                {[...tree.folders.entries()].map(([name, node]) => (
                  <TreeFolder key={name} name={name} node={node} depth={0} onSelect={onSelectFile} />
                ))}
                {tree.files.map((item) => (
                  <button
                    type="button"
                    className="file-tree-row file-tree-file"
                    onClick={() => onSelectFile(item.path)}
                    key={item.path}
                  >
                    <FileCode2 size={16} /><span>{item.name}</span>
                  </button>
                ))}
                {truncated && <p className="file-tree-note">文件较多，仅显示前 10,000 个。</p>}
              </div>
            </aside>
            <main className="file-content-view">
              {loading ? (
                <div className="file-panel-empty">正在读取…</div>
              ) : file?.binary ? (
                <div className="file-panel-empty">这是二进制文件，无法在此预览。</div>
              ) : file ? (
                <>
                  <header><strong>{file.path}</strong><span>{file.size} B</span></header>
                  <pre>{file.content}</pre>
                  {file.truncated && <footer>文件过大，当前仅显示前 1 MB。</footer>}
                </>
              ) : (
                <div className="file-panel-empty">从左侧选择文件查看内容。</div>
              )}
            </main>
          </div>
        )}
      </section>
    </div>
  );
}
