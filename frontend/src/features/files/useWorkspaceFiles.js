import { useCallback, useState } from "react";

import { request } from "../../api/client";

export default function useWorkspaceFiles({
  token,
  workspace,
  runnerOnline,
  clientId,
  agentBusy,
  showToast,
  onBranchChanged,
  onPanelOpen,
  onReview,
  onPromptLine,
}) {
  const [gitState, setGitState] = useState(null);
  const [gitDiff, setGitDiff] = useState(null);
  const [gitOpen, setGitOpen] = useState(false);
  const [gitView, setGitView] = useState("changes");
  const [gitLoading, setGitLoading] = useState(false);
  const [gitBusy, setGitBusy] = useState(false);
  const [filePanelOpen, setFilePanelOpen] = useState(false);
  const [filePanelMode, setFilePanelMode] = useState("changed");
  const [projectFiles, setProjectFiles] = useState([]);
  const [projectFilesTruncated, setProjectFilesTruncated] = useState(false);
  const [selectedFilePath, setSelectedFilePath] = useState("");
  const [selectedFile, setSelectedFile] = useState(null);
  const [filePanelLoading, setFilePanelLoading] = useState(false);
  const workspaceId = workspace?.workspace_id || "";

  const refreshGit = useCallback(async () => {
    if (!workspace || !runnerOnline) return null;
    setGitLoading(true);
    try {
      const data = await request(
        token,
        `/api/git/status?workspace_id=${encodeURIComponent(workspaceId)}`,
      );
      setGitState(data);
      return data;
    } finally {
      setGitLoading(false);
    }
  }, [runnerOnline, token, workspace, workspaceId]);

  const loadGitDiff = useCallback(async (
    scope = gitView,
    path = "",
    base = "",
  ) => {
    if (!workspace || !runnerOnline) return null;
    setGitLoading(true);
    try {
      const data = await request(token, "/api/git/diff", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: workspaceId,
          scope,
          path,
          base,
        }),
      });
      setGitDiff(data);
      return data;
    } catch (error) {
      showToast(error.message);
      return null;
    } finally {
      setGitLoading(false);
    }
  }, [gitView, runnerOnline, showToast, token, workspace, workspaceId]);

  async function openGit(view = "changes") {
    if (!workspace) return;
    onPanelOpen();
    setGitOpen(true);
    setGitView(view);
    setGitDiff(null);
    try {
      const latest = await refreshGit();
      if (!latest?.available) return;
      await loadGitDiff(view, "", view === "compare" ? latest.default_base : "");
    } catch (error) {
      showToast(error.message);
    }
  }

  async function loadProjectFiles() {
    if (!workspace || !runnerOnline) return [];
    setFilePanelLoading(true);
    try {
      const data = await request(
        token,
        `/api/files?workspace_id=${encodeURIComponent(workspaceId)}`,
      );
      setProjectFiles(data.files || []);
      setProjectFilesTruncated(Boolean(data.truncated));
      return data.files || [];
    } catch (error) {
      showToast(error.message);
      return [];
    } finally {
      setFilePanelLoading(false);
    }
  }

  async function openFilePanel(mode = "changed", path = "") {
    if (!workspace) return;
    onPanelOpen();
    setFilePanelOpen(true);
    setFilePanelMode(mode);
    setSelectedFilePath(path);
    setSelectedFile(null);
    if (mode === "changed") {
      setGitDiff(null);
      const latest = await refreshGit();
      if (path && latest?.changes?.some((item) => item.path === path)) {
        await loadGitDiff("changes", path);
      }
      return;
    }
    if (!projectFiles.length) await loadProjectFiles();
  }

  async function selectProjectFile(path) {
    if (!workspace) return;
    setSelectedFilePath(path);
    setSelectedFile(null);
    setFilePanelLoading(true);
    try {
      const data = await request(token, "/api/files/read", {
        method: "POST",
        body: JSON.stringify({ workspace_id: workspaceId, path }),
      });
      setSelectedFile(data);
    } catch (error) {
      showToast(error.message);
    } finally {
      setFilePanelLoading(false);
    }
  }

  async function changeFilePanelMode(mode) {
    setFilePanelMode(mode);
    setSelectedFilePath("");
    setSelectedFile(null);
    setGitDiff(null);
    if (mode === "all" && !projectFiles.length) await loadProjectFiles();
  }

  async function handleGitView(view, base = "") {
    setGitView(view);
    setGitDiff(null);
    await loadGitDiff(view, "", view === "compare" ? base : "");
  }

  async function handleGitAction(action, values = {}) {
    if (!workspace || gitBusy || agentBusy) return false;
    setGitBusy(true);
    try {
      const result = await request(token, "/api/git/action", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: workspaceId,
          action,
          paths: values.paths || [],
          branch: values.branch || "",
          message: values.message || "",
        }),
      });
      setGitState(result.git);
      const labels = {
        stage: "文件已暂存",
        unstage: "已取消暂存",
        switch: `已切换到 ${result.git.branch}`,
        create_branch: `已创建并切换到 ${result.git.branch}`,
        commit: "提交已创建",
        push: "当前分支已推送",
      };
      showToast(labels[action] || "Git 操作完成");
      if (action === "switch" || action === "create_branch") {
        try {
          await request(token, "/api/reset", {
            method: "POST",
            body: JSON.stringify({
              client_id: clientId,
              workspace_id: workspaceId,
            }),
          });
        } catch (error) {
          if (error.status !== 503) showToast(error.message);
        }
        await onBranchChanged();
        setGitView("changes");
        await loadGitDiff("changes");
      } else if (action !== "push") {
        await loadGitDiff(
          gitView,
          action === "commit" ? "" : gitDiff?.path || "",
          gitDiff?.base || "",
        );
      }
      return true;
    } catch (error) {
      showToast(error.message);
      return false;
    } finally {
      setGitBusy(false);
    }
  }

  function launchGitReview(view, base, path) {
    if (agentBusy) return;
    setGitOpen(false);
    onReview({ view, base, path });
  }

  function addDiffLineToPrompt(path, line) {
    setGitOpen(false);
    onPromptLine(path, line);
  }

  async function selectChangedFile(path) {
    setSelectedFilePath(path);
    await loadGitDiff("changes", path);
  }

  return {
    gitState,
    gitDiff,
    gitOpen,
    gitView,
    gitLoading,
    gitBusy,
    filePanelOpen,
    filePanelMode,
    projectFiles,
    projectFilesTruncated,
    selectedFilePath,
    selectedFile,
    filePanelLoading,
    addDiffLineToPrompt,
    applyGitState: setGitState,
    changeFilePanelMode,
    closeFilePanel: () => setFilePanelOpen(false),
    closeGit: () => setGitOpen(false),
    handleGitAction,
    handleGitView,
    launchGitReview,
    loadGitDiff,
    openFilePanel,
    openGit,
    refreshGit,
    selectChangedFile,
    selectProjectFile,
  };
}
