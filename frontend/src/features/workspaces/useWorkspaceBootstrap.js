import { useCallback, useEffect, useMemo, useState } from "react";

import { request } from "../../api/client";
import {
  clientIdFor,
  TOKEN_KEY,
  WORKSPACE_KEY,
} from "../../app/storage";

const EMPTY_BOOTSTRAP = Object.freeze({
  model: "",
  workspaces: [],
  version: "",
  context_window_tokens: 0,
});

export default function useWorkspaceBootstrap(showToast) {
  const [token, setToken] = useState(
    () => localStorage.getItem(TOKEN_KEY) || "",
  );
  const [authState, setAuthState] = useState("checking");
  const [authError, setAuthError] = useState("");
  const [bootstrap, setBootstrap] = useState(EMPTY_BOOTSTRAP);
  const [selectedId, setSelectedId] = useState(
    () => localStorage.getItem(WORKSPACE_KEY) || "",
  );
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [runnerOnline, setRunnerOnline] = useState(false);

  const selectedWorkspace = useMemo(
    () => bootstrap.workspaces.find(
      (item) => item.workspace_id === selectedId,
    ) || null,
    [bootstrap.workspaces, selectedId],
  );
  const clientId = selectedWorkspace
    ? clientIdFor(selectedWorkspace.workspace_id)
    : "";

  const loadBootstrap = useCallback(async (activeToken) => {
    const data = await request(activeToken, "/api/bootstrap");
    setBootstrap(data);
    setRunnerOnline(true);
    const saved = localStorage.getItem(WORKSPACE_KEY);
    const savedExists = data.workspaces.some(
      (item) => item.workspace_id === saved,
    );
    if (savedExists) {
      setSelectedId(saved);
    } else {
      setSelectedId("");
      setProjectPickerOpen(true);
    }
    return data;
  }, []);

  useEffect(() => {
    let ignore = false;
    async function verifySavedToken() {
      if (!token) {
        setAuthState("logged-out");
        return;
      }
      try {
        await request(token, "/api/auth/verify", { method: "POST" });
        if (ignore) return;
        setAuthState("ready");
        try {
          await loadBootstrap(token);
        } catch (error) {
          if (!ignore) {
            setRunnerOnline(false);
            showToast(error.message);
          }
        }
      } catch {
        if (ignore) return;
        localStorage.removeItem(TOKEN_KEY);
        setToken("");
        setAuthState("logged-out");
        setAuthError("保存的访问令牌已失效，请重新输入。");
      }
    }
    verifySavedToken();
    return () => {
      ignore = true;
    };
  }, [loadBootstrap, showToast, token]);

  useEffect(() => {
    if (authState !== "ready") return undefined;
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch("/api/health", { cache: "no-store" });
        const health = await response.json();
        const wasOffline = !runnerOnline;
        setRunnerOnline(Boolean(health.runner_connected));
        if (health.runner_connected && wasOffline) {
          await loadBootstrap(token);
          showToast("本机 Runner 已重新连接");
        }
      } catch {
        setRunnerOnline(false);
      }
    }, 15000);
    return () => window.clearInterval(timer);
  }, [authState, loadBootstrap, runnerOnline, showToast, token]);

  async function login(candidate) {
    setAuthError("");
    setAuthState("checking");
    try {
      await request(candidate, "/api/auth/verify", { method: "POST" });
      localStorage.setItem(TOKEN_KEY, candidate);
      setToken(candidate);
      setAuthState("ready");
      try {
        await loadBootstrap(candidate);
      } catch (error) {
        setRunnerOnline(false);
        showToast(error.message);
      }
    } catch (error) {
      setAuthState("logged-out");
      setAuthError(error.message);
    }
  }

  function selectWorkspace(workspace) {
    localStorage.setItem(WORKSPACE_KEY, workspace.workspace_id);
    setSelectedId(workspace.workspace_id);
    setProjectPickerOpen(false);
  }

  async function openProjectPicker() {
    setProjectPickerOpen(true);
    if (!runnerOnline) return;
    try {
      await loadBootstrap(token);
    } catch (error) {
      setRunnerOnline(false);
      showToast(error.message);
    }
  }

  function logout(message = "") {
    localStorage.removeItem(TOKEN_KEY);
    setToken("");
    setAuthState("logged-out");
    setAuthError(message);
  }

  return {
    token,
    authState,
    authError,
    bootstrap,
    selectedId,
    selectedWorkspace,
    clientId,
    projectPickerOpen,
    runnerOnline,
    closeProjectPicker: () => setProjectPickerOpen(false),
    login,
    logout,
    openProjectPicker,
    selectWorkspace,
  };
}
