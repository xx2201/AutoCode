import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileCode2,
  Folder,
  FolderOpen,
  Search,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import { parseDiffHunks } from "./diff";
import { highlightCodeLines } from "./syntax";

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

function CodeLine({ className = "", html, lineNumber, marker = "" }) {
  return (
    <div className={`file-code-line ${className}`}>
      <span>{lineNumber}</span>
      {marker !== null && <i>{marker}</i>}
      <code dangerouslySetInnerHTML={{ __html: html || "&nbsp;" }} />
    </div>
  );
}

function DiffHunk({ hunk, path }) {
  const highlighted = useMemo(
    () => highlightCodeLines(hunk.lines.map((line) => line.text).join("\n"), path),
    [hunk.lines, path],
  );
  const end = Math.max(hunk.newStart, hunk.newStart + hunk.newCount - 1);
  return (
    <section className="file-diff-hunk">
      <header>
        <span><ChevronDown size={16} /> 第 {hunk.newStart}-{end} 行</span>
        <span><b>+{hunk.additions}</b><i>−{hunk.deletions}</i></span>
      </header>
      {hunk.lines.map((line, index) => (
        <CodeLine
          className={line.kind}
          html={highlighted[index]}
          key={`${index}-${line.old}-${line.next}`}
          lineNumber={line.next || line.old}
          marker={line.marker}
        />
      ))}
    </section>
  );
}

function DiffContent({ diff }) {
  const hunks = useMemo(() => parseDiffHunks(diff?.diff), [diff?.diff]);
  if (!diff?.diff) {
    return <div className="file-panel-empty">选择一个已修改文件查看内容。</div>;
  }
  const changedFile = diff.files?.[0] || {};
  return (
    <div className="file-diff-view">
      <header className="file-diff-header">
        <strong title={diff.path}>{diff.path}</strong>
        <span>
          <b>+{changedFile.additions || 0}</b>
          <i>−{changedFile.deletions || 0}</i>
        </span>
      </header>
      <div className="file-diff-code">
        {hunks.map((hunk) => (
          <DiffHunk
            hunk={hunk}
            key={`${hunk.oldStart}-${hunk.newStart}`}
            path={diff.path}
          />
        ))}
      </div>
      {diff.truncated && <p>Diff 过大，当前仅显示前 2 MB。</p>}
    </div>
  );
}

function FileContent({ file }) {
  const highlighted = useMemo(
    () => highlightCodeLines(file?.content || "", file?.path || ""),
    [file?.content, file?.path],
  );
  if (!file) return null;
  return (
    <>
      <header><strong>{file.path}</strong><span>{file.size} B</span></header>
      <div className="file-source-code" aria-label={`${file.path} 代码`}>
        {highlighted.map((html, index) => (
          <CodeLine
            html={html}
            key={index}
            lineNumber={index + 1}
            marker={null}
          />
        ))}
      </div>
      {file.truncated && <footer>文件过大，当前仅显示前 1 MB。</footer>}
    </>
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
  const additions = changedFiles.reduce(
    (total, item) => total + Number(item.additions || 0),
    0,
  );
  const deletions = changedFiles.reduce(
    (total, item) => total + Number(item.deletions || 0),
    0,
  );

  return (
    <div className="file-panel-layer" role="dialog" aria-modal="true" aria-label="项目文件">
      <button className="file-panel-backdrop" type="button" aria-label="关闭" onClick={onClose} />
      <section className="file-panel">
        <header>
          <button type="button" onClick={onClose} aria-label="关闭"><X size={21} /></button>
          <div className="file-panel-heading">
            <strong title={title}>{title}</strong>
            {mode === "changed" && (
              <span><b>+{additions}</b><i>−{deletions}</i></span>
            )}
          </div>
          <button type="button" onClick={onOpenGit} aria-label="打开 Git 管理" title="Git 管理">
            <ExternalLink size={19} />
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
                <FileContent file={file} />
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
