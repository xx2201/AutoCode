import { FileCode2, Menu, Zap } from "lucide-react";

import ContextMeter from "../workspaces/ContextMeter";

export default function WorkspaceHeader({
  selectedWorkspace,
  runnerOnline,
  model,
  contextUsage,
  onOpenMobile,
  onOpenModelSettings,
}) {
  return (
    <header className="workspace-header">
      <button
        className="mobile-menu"
        type="button"
        onClick={onOpenMobile}
        aria-label="打开菜单"
      >
        <Menu size={21} />
      </button>
      <div className="workspace-title">
        <div className="project-mini-icon"><FileCode2 size={20} /></div>
        <div>
          <strong>{selectedWorkspace?.name || "尚未选择项目"}</strong>
          <small>{selectedWorkspace?.path || "从项目列表中打开一个 workspace"}</small>
        </div>
      </div>
      <div className="header-meta">
        <span className={`connection-pill ${runnerOnline ? "online" : "offline"}`}>
          <i /> {runnerOnline ? "Connected" : "Offline"}
        </span>
        <button
          className="model-pill"
          type="button"
          onClick={onOpenModelSettings}
          aria-label="打开模型设置"
          title="模型设置"
        >
          <Zap size={14} /> {model || "model"}
        </button>
        <ContextMeter usage={contextUsage} />
      </div>
    </header>
  );
}
