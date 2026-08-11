import {
  Activity,
  ChevronDown,
  Code2,
  FileDiff,
  Folder,
  FolderGit2,
  GitBranch,
  History,
  ListTodo,
  LogOut,
  MessageSquareText,
  PanelLeftClose,
  TerminalSquare,
} from "lucide-react";

export default function Sidebar({
  mobileOpen,
  selectedWorkspace,
  sessionsCount,
  gitState,
  runnerOnline,
  onCloseMobile,
  onOpenProjects,
  onOpenInfo,
  onOpenSessions,
  onOpenFiles,
  onOpenGit,
  onLogout,
}) {
  return (
    <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}>
      <div className="sidebar-brand">
        <span className="logo-mark"><Code2 size={20} /></span>
        <div><strong>AutoCode</strong><small>Local Agent</small></div>
        <button
          className="sidebar-close"
          type="button"
          onClick={onCloseMobile}
          aria-label="关闭菜单"
        >
          <PanelLeftClose size={19} />
        </button>
      </div>
      <button className="workspace-switcher" type="button" onClick={onOpenProjects}>
        <span className="workspace-icon"><FolderGit2 size={19} /></span>
        <span>
          <small>当前项目</small>
          <strong>{selectedWorkspace?.name || "选择项目"}</strong>
        </span>
        <ChevronDown size={17} />
      </button>
      <nav className="side-nav">
        <span className="nav-caption">WORKSPACE</span>
        <button className="active" type="button" onClick={onCloseMobile}>
          <MessageSquareText size={18} /> 对话
        </button>
        <button type="button" onClick={() => onOpenInfo("turn")}>
          <ListTodo size={18} /> 当前任务
        </button>
        <button type="button" onClick={() => onOpenInfo("trace")}>
          <Activity size={18} /> 运行 Trace
        </button>
        <button type="button" onClick={() => onOpenInfo("diagnostics")}>
          <TerminalSquare size={18} /> 本地诊断
        </button>
        <button type="button" onClick={onOpenSessions}>
          <History size={18} /> 历史会话
          {sessionsCount > 0 && <em>{sessionsCount}</em>}
        </button>
        <button type="button" onClick={() => onOpenFiles("all")}>
          <Folder size={18} /> 文件
        </button>
      </nav>
      <nav className="side-nav git-side-nav">
        <span className="nav-caption">GIT</span>
        <button type="button" onClick={() => onOpenFiles("changed")}>
          <FileDiff size={18} /> Changes
          {gitState?.available && (
            <span className="sidebar-git-stats">
              <b>+{gitState.additions}</b>
              <i>-{gitState.deletions}</i>
            </span>
          )}
        </button>
        <button type="button" onClick={() => onOpenGit("changes")}>
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
        <button className="logout-link" type="button" onClick={onLogout}>
          <LogOut size={17} /> 退出登录
        </button>
      </div>
    </aside>
  );
}
