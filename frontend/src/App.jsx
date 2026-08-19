import { Check, Code2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { encodeAttachment, isTransientRestoreError, request, requestSessionResume, streamRequest } from "./api/client";
import { createInteractionId, PERMISSION_PRESET_KEY, renewClientId } from "./app/storage";
import useToast from "./app/useToast";
import FilePanel from "./FilePanel";
import GitPanel from "./GitPanel";
import { handleRunEvent } from "./features/agent-run/events";
import useAgentRun from "./features/agent-run/useAgentRun";
import { hasPendingApprovals } from "./features/approvals/ApprovalRequest";
import useAttachments, { releaseAttachmentPreviews } from "./features/attachments/useAttachments";
import LoginView from "./features/auth/LoginView";
import Composer from "./features/conversation/Composer";
import ConversationPane from "./features/conversation/ConversationPane";
import { mergeResultMessages } from "./features/conversation/model";
import useWorkspaceFiles from "./features/files/useWorkspaceFiles";
import useOutputFiles from "./features/files/useOutputFiles";
import InfoPanel from "./features/layout/InfoPanel";
import Sidebar from "./features/layout/Sidebar";
import WorkspaceHeader from "./features/layout/WorkspaceHeader";
import SessionPanel, { DeleteSessionDialog } from "./features/sessions/SessionPanel";
import ProjectPicker from "./features/workspaces/ProjectPicker";
import useWorkspaceBootstrap from "./features/workspaces/useWorkspaceBootstrap";
import {
  clearPageSessionId,
  createSessionRequestCoordinator,
  readPageSessionId,
  storePageSessionId,
} from "./session-history";
import {
  applyTurnLifecycle,
  consumePendingInput,
  createPendingInput,
  groupConversation,
  latestEditableTurnId,
  normalizeChangeAction,
  settlePendingInput,
} from "./conversation";

export default function App() {
  const { toast, showToast } = useToast();
  const workspaceBootstrap = useWorkspaceBootstrap(showToast);
  const {
    token,
    authState,
    authError,
    bootstrap,
    selectedId,
    selectedWorkspace,
    clientId,
    projectPickerOpen,
    runnerOnline,
    closeProjectPicker,
    login,
    logout,
    openProjectPicker,
    selectWorkspace: selectWorkspaceBase,
  } = workspaceBootstrap;
  const [permissionPreset, setPermissionPreset] = useState(
    () => localStorage.getItem(PERMISSION_PRESET_KEY) || "workspace-write",
  );
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [pending, setPending] = useState(null);
  const [approvalBusyIds, setApprovalBusyIds] = useState({});
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);
  const [resumingSessionId, setResumingSessionId] = useState("");
  const [pageSessionRestoring, setPageSessionRestoring] = useState(
    () => Boolean(selectedId && readPageSessionId(window.history, selectedId)),
  );
  const [activeSessionId, setActiveSessionId] = useState("");
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [deletingSessionId, setDeletingSessionId] = useState("");
  const [status, setStatus] = useState("idle");
  const run = useAgentRun();
  const {
    streamText,
    stage: runStage,
    work: liveWork,
    startedAt: runStartedAt,
    activeTurnId,
  } = run;
  const [deliveryMode, setDeliveryMode] = useState("steer");
  const [pendingInputs, setPendingInputs] = useState([]);
  const [editingTurnId, setEditingTurnId] = useState("");
  const [editDraft, setEditDraft] = useState("");
  const [editBusy, setEditBusy] = useState(false);
  const [changeActionStates, setChangeActionStates] = useState({});
  const [contextUsage, setContextUsage] = useState({
    used_tokens: 0,
    window_tokens: 0,
  });
  const {
    attachments,
    addAttachments,
    clearAttachments,
    removeAttachment,
  } = useAttachments(showToast);
  const [panel, setPanel] = useState(null);
  const [panelContent, setPanelContent] = useState("");
  const messageEndRef = useRef(null);
  const fileInputRef = useRef(null);
  const promptInputRef = useRef(null);
  const sessionRequestsRef = useRef(null);
  if (!sessionRequestsRef.current) {
    sessionRequestsRef.current = createSessionRequestCoordinator(
      selectedWorkspace?.workspace_id || "",
    );
  }

  const conversationTurns = useMemo(() => groupConversation(messages), [messages]);
  const editableTurnId = useMemo(
    () => latestEditableTurnId(conversationTurns, busy, status),
    [busy, conversationTurns, status],
  );

  const files = useWorkspaceFiles({
    token,
    workspace: selectedWorkspace,
    runnerOnline,
    clientId,
    agentBusy: busy,
    showToast,
    onPanelOpen: () => setMobileNavOpen(false),
    onBranchChanged: async () => {
      renewClientId(selectedWorkspace.workspace_id);
      setMessages([]);
      setPending(null);
      setStatus("idle");
    },
    onReview: ({ view, base, path }) => {
      const range = view === "compare"
        ? `当前分支相对 ${base} 的变更`
        : "当前未提交变更";
      const target = path ? `，重点审查文件 ${path}` : "";
      submitPrompt(
        `请以代码审查者身份 Review ${range}${target}。优先发现会导致错误、回归、数据损坏、`
        + "安全问题或缺失测试的具体问题；按严重程度列出发现，并提供文件和行号。"
        + "如果没有发现问题，请明确说明剩余测试风险。",
      );
    },
    onPromptLine: (path, line) => {
      const lineNumber = line.next || line.old;
      setPrompt(
        `请重点检查 ${path}:${lineNumber} 这一处变更，解释它是否存在缺陷或回归风险：\n`
        + line.text,
      );
      window.requestAnimationFrame(() => promptInputRef.current?.focus());
    },
  });
  const {
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
    applyGitState,
    changeFilePanelMode,
    closeFilePanel,
    closeGit,
    handleGitAction,
    handleGitView,
    launchGitReview,
    loadGitDiff,
    openFilePanel,
    openGit,
    refreshGit,
    selectChangedFile,
    selectProjectFile,
  } = files;
  const { downloadingFileId, handleOutputFile } = useOutputFiles({
    token,
    workspace: selectedWorkspace,
    clientId,
    showToast,
    onUnauthorized: () => logout("访问令牌已失效。"),
  });

  const applyResumedSession = useCallback((workspaceId, sessionId, data) => {
    const restoredPreset = data.result?.permission_preset;
    if (["workspace-write", "danger-full-access"].includes(restoredPreset)) {
      setPermissionPreset(restoredPreset);
      localStorage.setItem(PERMISSION_PRESET_KEY, restoredPreset);
    }
    setMessages(data.messages || []);
    setPending(hasPendingApprovals(data.result) ? data.result : null);
    setStatus(data.result?.status || "idle");
    setContextUsage({
      used_tokens: data.result?.context_used_tokens || 0,
      window_tokens:
        data.result?.context_window_tokens
        || bootstrap.context_window_tokens
        || 0,
    });
    setActiveSessionId(sessionId);
    storePageSessionId(window.history, workspaceId, sessionId);
    setPanel(null);
  }, [bootstrap.context_window_tokens]);

  const refreshSessions = useCallback(async () => {
    if (!selectedWorkspace || !runnerOnline) {
      setSessionsLoading(false);
      return [];
    }
    const workspaceId = selectedWorkspace.workspace_id;
    const requestTicket = sessionRequestsRef.current.begin(workspaceId);
    setSessionsLoading(true);
    try {
      const data = await request(
        token,
        `/api/sessions?workspace_id=${encodeURIComponent(workspaceId)}`,
      );
      if (!sessionRequestsRef.current.isCurrent(requestTicket)) return [];
      const nextSessions = data.sessions || [];
      setSessions(nextSessions);
      return nextSessions;
    } catch (error) {
      if (!sessionRequestsRef.current.isCurrent(requestTicket)) return [];
      throw error;
    } finally {
      if (sessionRequestsRef.current.isCurrent(requestTicket)) {
        setSessionsLoading(false);
      }
    }
  }, [runnerOnline, selectedWorkspace, token]);

  const refreshMessages = useCallback(async () => {
    if (!selectedWorkspace || !runnerOnline) return [];
    const data = await request(
      token,
      `/api/messages/${clientId}?workspace_id=${encodeURIComponent(selectedWorkspace.workspace_id)}`,
    );
    return data.messages || [];
  }, [clientId, runnerOnline, selectedWorkspace, token]);

  useEffect(() => {
    setContextUsage((current) => ({
      used_tokens: current.used_tokens,
      window_tokens: bootstrap.context_window_tokens || current.window_tokens,
    }));
  }, [bootstrap.context_window_tokens]);

  useEffect(() => {
    // Runner 重启期间会短暂离线。此时会话列表不可用，不能把空列表
    // 误判为当前页面的 session 已被删除并清除续接标识。
    if (!selectedWorkspace || !runnerOnline) return;
    let ignore = false;
    async function initializeWorkspace() {
      const workspaceId = selectedWorkspace.workspace_id;
      const pageSessionId = readPageSessionId(window.history, workspaceId);
      setPageSessionRestoring(Boolean(pageSessionId));
      sessionRequestsRef.current.selectWorkspace(workspaceId);
      const restoreClientId = renewClientId(workspaceId);
      setMessages([]);
      setSessions([]);
      setSessionsLoading(true);
      setPendingInputs([]);
      setEditingTurnId("");
      setChangeActionStates({});
      setPending(null);
      setStatus("idle");
      setContextUsage({
        used_tokens: 0,
        window_tokens: bootstrap.context_window_tokens || 0,
      });
      setActiveSessionId("");
      try {
        const [availableSessions] = await Promise.all([refreshSessions(), refreshGit()]);
        if (ignore) return;
        if (!pageSessionId) return;
        if (!availableSessions.some((session) => session.session_id === pageSessionId)) {
          clearPageSessionId(window.history);
          return;
        }
        setBusy(true);
        setResumingSessionId(pageSessionId);
        try {
          const data = await requestSessionResume(token, {
            clientId: restoreClientId,
            workspaceId,
            sessionId: pageSessionId,
          });
          if (ignore) return;
          applyResumedSession(workspaceId, pageSessionId, data);
          showToast("已恢复上次会话");
        } catch (error) {
          if (ignore) return;
          if (!isTransientRestoreError(error)) {
            clearPageSessionId(window.history);
          }
          showToast(`自动恢复会话失败：${error.message}`);
        } finally {
          if (!ignore) {
            setResumingSessionId("");
            setBusy(false);
          }
        }
      } catch (error) {
        if (!ignore) showToast(error.message);
      } finally {
        if (!ignore) setPageSessionRestoring(false);
      }
    }
    initializeWorkspace();
    return () => {
      ignore = true;
    };
  }, [
    applyResumedSession,
    refreshGit,
    refreshSessions,
    selectedWorkspace,
    showToast,
    token,
  ]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy, pending]);

  function selectWorkspace(workspace) {
    clearPageSessionId(window.history);
    setPageSessionRestoring(false);
    sessionRequestsRef.current.selectWorkspace(workspace.workspace_id);
    setSessions([]);
    setSessionsLoading(true);
    selectWorkspaceBase(workspace);
    setMobileNavOpen(false);
    showToast(`已打开 ${workspace.name}`);
  }

  async function openSessionsPanel() {
    setPanel("sessions");
    try {
      await refreshSessions();
    } catch (error) {
      showToast(error.message);
    }
  }

  async function sendDuringRun(cleanPrompt) {
    const expectedTurnId = activeTurnId;
    if (!expectedTurnId) {
      showToast("尚未取得当前 Turn 标识，请稍后重试。");
      return;
    }
    const localId = createInteractionId("input");
    const localInput = createPendingInput(cleanPrompt, deliveryMode, localId);
    setPendingInputs((items) => [...items, localInput]);
    setPrompt("");
    try {
      const data = await request(token, "/api/turn/message", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          expected_turn_id: expectedTurnId,
          mode: deliveryMode,
          prompt: cleanPrompt,
        }),
      });
      const serverItem = data.queued_message;
      setPendingInputs((items) => items.map((item) => (
        item.id === localId
          ? {
              ...item,
              id: serverItem?.id || item.id,
              prompt: serverItem?.prompt || item.prompt,
              createdAt: serverItem?.created_at || "",
              status: data.accepted === false ? "rejected" : "accepted",
              detail: data.mode === "queue" ? "等待当前回答完成" : "将在下一安全点生效",
            }
          : item
      )));
    } catch (error) {
      setPendingInputs((items) => settlePendingInput(items, localId, "failed", error.message));
      setPrompt(cleanPrompt);
      showToast(error.message);
    }
  }

  function acceptTurnLifecycle(event) {
    setMessages((items) => applyTurnLifecycle(items, event));
    if (event.message_id) {
      setPendingInputs((items) => consumePendingInput(items, event.message_id));
    }
  }

  function beginEditTurn(turn) {
    setEditingTurnId(turn.id);
    setEditDraft(turn.user?.content || "");
  }

  async function saveEditedTurn(event, turn) {
    event.preventDefault();
    const cleanPrompt = editDraft.trim();
    if (!cleanPrompt || editBusy || busy || !selectedWorkspace) return;
    const turnId = turn.user?.turn_id || turn.id;
    const previousMessages = messages;
    const previousStatus = status;
    setMessages((items) => {
      const targetIndex = items.findLastIndex((message) => (
        message.role === "user" && (!message.turn_id || message.turn_id === turnId)
      ));
      if (targetIndex < 0) return items;
      return items.slice(0, targetIndex + 1).map((message, index) => (
        index === targetIndex ? { ...message, content: cleanPrompt } : message
      ));
    });
    setEditingTurnId("");
    setEditBusy(true);
    setBusy(true);
    setStatus("running");
    run.begin(turnId);
    try {
      let payload = null;
      await streamRequest(token, "/api/turn/edit/stream", {
        client_id: clientId,
        workspace_id: selectedWorkspace.workspace_id,
        turn_id: turnId,
        prompt: cleanPrompt,
        permission_preset: permissionPreset,
      }, (streamEvent) => {
        handleRunEvent(streamEvent, {
          run,
          onTurnLifecycle: acceptTurnLifecycle,
          onResult: (data) => {
            payload = data;
          },
          errorMessage: "重新回答失败。",
        });
      });
      if (!payload) throw new Error("流式响应结束但没有最终结果。");
      const result = payload.result || payload;
      let synchronized = payload.messages || [];
      try {
        const refreshed = await refreshMessages();
        if (refreshed.length > 0) synchronized = refreshed;
      } catch (error) {
        showToast(`操作记录刷新失败：${error.message}`);
      }
      setMessages((items) => mergeResultMessages(
        items,
        synchronized,
        result,
        payload.changed_files || result.changed_files || [],
      ));
      setPending(hasPendingApprovals(result) ? result : null);
      setStatus(result.status || "completed");
      setContextUsage({
        used_tokens: result.context_used_tokens || 0,
        window_tokens: result.context_window_tokens || bootstrap.context_window_tokens || 0,
      });
      showToast("已根据编辑后的提问重新回答，工作区文件保持原状");
      refreshGit().catch((error) => showToast(`Git 刷新失败：${error.message}`));
    } catch (error) {
      setMessages(previousMessages);
      setStatus(previousStatus);
      setEditingTurnId(turn.id);
      showToast(error.message);
    } finally {
      setEditBusy(false);
      setBusy(false);
      run.reset();
    }
  }

  async function changeTurnFiles(turn, action) {
    if (!selectedWorkspace) return;
    const turnId = turn.user?.turn_id || turn.id;
    setChangeActionStates((states) => ({
      ...states,
      [turn.id]: { ...states[turn.id], busy: true, busyAction: action, conflict: false, detail: "" },
    }));
    try {
      const data = await request(token, "/api/changes/action", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          turn_id: turnId,
          action,
        }),
      });
      const normalized = normalizeChangeAction(data, action);
      setChangeActionStates((states) => ({
        ...states,
        [turn.id]: {
          ...normalized,
          canUndo: data.can_undo,
          canReapply: data.can_reapply,
          detail: data.blocked_reason || normalized.detail,
          busy: false,
          busyAction: "",
        },
      }));
      if (data.git) applyGitState(data.git);
      else refreshGit().catch((error) => showToast(`Git 刷新失败：${error.message}`));
    } catch (error) {
      setChangeActionStates((states) => ({
        ...states,
        [turn.id]: {
          ...states[turn.id],
          busy: false,
          busyAction: "",
          conflict: error.status === 409,
          detail: error.message,
        },
      }));
      showToast(error.message);
    }
  }

  async function submitPrompt(value = prompt) {
    const cleanPrompt = value.trim();
    if ((!cleanPrompt && attachments.length === 0) || !selectedWorkspace) return;
    if (!runnerOnline) {
      showToast("本机 Runner 离线，暂时不能执行任务。");
      return;
    }
    if (busy) {
      if (attachments.length > 0) {
        showToast("运行中的引导消息暂不支持附件，请等待当前回答结束。");
        return;
      }
      await sendDuringRun(cleanPrompt);
      return;
    }
    const pendingAttachments = attachments;
    const continuedSessionId = readPageSessionId(
      window.history,
      selectedWorkspace.workspace_id,
    ) || activeSessionId;
    const attachmentSummary = pendingAttachments.map(({ file }) => ({
      name: file.name,
      media_type: file.type || "application/octet-stream",
      size: file.size,
    }));
    setMessages((items) => [
      ...items,
      {
        role: "user",
        content: cleanPrompt || "请分析我上传的文件。",
        attachments: attachmentSummary,
      },
    ]);
    setPrompt("");
    clearAttachments();
    setBusy(true);
    setStatus("running");
    run.begin();
    try {
      const encodedAttachments = await Promise.all(
        pendingAttachments.map(({ file }) => encodeAttachment(file)),
      );
      let result = null;
      await streamRequest(
        token,
        "/api/chat/stream",
        {
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          prompt: cleanPrompt,
          attachments: encodedAttachments,
          permission_preset: permissionPreset,
          session_id: continuedSessionId,
        },
        (event) => {
          handleRunEvent(event, {
            run,
            onTurnLifecycle: acceptTurnLifecycle,
            onResult: (data) => {
              result = data;
            },
            errorMessage: "本机 Runner 执行失败。",
          });
        },
      );
      if (!result) throw new Error("流式响应结束但没有最终结果。");
      let synchronized = [];
      try {
        synchronized = await refreshMessages();
      } catch (error) {
        showToast(`操作记录刷新失败：${error.message}`);
      }
      setMessages((items) => mergeResultMessages(items, synchronized, result));
      setPending(hasPendingApprovals(result) ? result : null);
      setPendingInputs([]);
      setStatus(result.status || "completed");
      setContextUsage({
        used_tokens: result.context_used_tokens || 0,
        window_tokens:
          result.context_window_tokens
          || bootstrap.context_window_tokens
          || 0,
      });
      if (result.session_id) {
        setActiveSessionId(result.session_id);
        storePageSessionId(
          window.history,
          selectedWorkspace.workspace_id,
          result.session_id,
        );
      }
      setBusy(false);
      run.reset();
      try {
        await refreshSessions();
      } catch (error) {
        showToast(`会话列表刷新失败：${error.message}`);
      }
      refreshGit().catch((error) => showToast(`Git 刷新失败：${error.message}`));
    } catch (error) {
      setMessages((items) => [
        ...items,
        { role: "assistant", content: `请求失败：${error.message}` },
      ]);
      setStatus("failed");
      if (error.status === 401) logout("访问令牌已失效。");
    } finally {
      setBusy(false);
      run.reset();
      releaseAttachmentPreviews(pendingAttachments);
    }
  }

  async function continueApprovalBatch(batch) {
    setBusy(true);
    run.resetTimeline();
    try {
      let result = null;
      await streamRequest(token, "/api/turn/continue/stream", {
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          expected_turn_id: batch.turn_id || pending.turn_id,
          batch_id: batch.batch_id || pending.approval_batch_id,
        }, (event) => {
          handleRunEvent(event, {
            run,
            onTurnLifecycle: acceptTurnLifecycle,
            onResult: (data) => {
              result = data;
            },
            errorMessage: "审批后继续执行失败。",
          });
      });
      if (!result) throw new Error("审批流结束但没有最终结果。");
      let synchronized = [];
      try {
        synchronized = await refreshMessages();
      } catch (error) {
        showToast(`操作记录刷新失败：${error.message}`);
      }
      setMessages((items) => mergeResultMessages(items, synchronized, result));
      setPending(hasPendingApprovals(result) ? result : null);
      setStatus(result.status || "completed");
      setContextUsage({
        used_tokens: result.context_used_tokens || 0,
        window_tokens:
          result.context_window_tokens
          || bootstrap.context_window_tokens
          || 0,
      });
      await refreshSessions();
    } catch (error) {
      showToast(error.message);
    } finally {
      setBusy(false);
      run.reset();
    }
  }

  async function resolveApproval(approval, action) {
    if (!pending || busy || !selectedWorkspace || !approval?.approval_id) return;
    setApprovalBusyIds((items) => ({ ...items, [approval.approval_id]: true }));
    try {
      const batch = await request(token, "/api/approval/decision", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          approval_id: approval.approval_id,
          expected_turn_id: pending.turn_id,
          batch_id: pending.approval_batch_id,
          action,
        }),
      });
      setPending((current) => (
        current
          ? {
              ...current,
              approval_batch_id: batch.batch_id,
              pending_approvals: batch.approvals,
            }
          : current
      ));
      if (batch.ready) {
        await continueApprovalBatch(batch);
      }
    } catch (error) {
      showToast(error.message);
    } finally {
      setApprovalBusyIds((items) => {
        const next = { ...items };
        delete next[approval.approval_id];
        return next;
      });
    }
  }

  async function changePermissionPreset(nextPreset) {
    if (!selectedWorkspace || nextPreset === permissionPreset) return;
    const previous = permissionPreset;
    setPermissionPreset(nextPreset);
    localStorage.setItem(PERMISSION_PRESET_KEY, nextPreset);
    try {
      await request(token, "/api/permission-preset", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          permission_preset: nextPreset,
        }),
      });
      showToast(nextPreset === "danger-full-access" ? "已启用完全访问" : "已限制为工作区写入");
    } catch (error) {
      setPermissionPreset(previous);
      localStorage.setItem(PERMISSION_PRESET_KEY, previous);
      showToast(error.message);
    }
  }

  async function resumeSession(sessionId) {
    if (busy || !selectedWorkspace) return;
    setBusy(true);
    setPendingInputs([]);
    setEditingTurnId("");
    setChangeActionStates({});
    setResumingSessionId(sessionId);
    try {
      const workspaceId = selectedWorkspace.workspace_id;
      const data = await requestSessionResume(token, {
        clientId,
        workspaceId,
        sessionId,
      });
      applyResumedSession(workspaceId, sessionId, data);
      showToast("会话已恢复");
    } catch (error) {
      showToast(error.message);
    } finally {
      setResumingSessionId("");
      setBusy(false);
    }
  }

  async function newSession() {
    if (!selectedWorkspace) return;
    try {
      await request(token, "/api/reset", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
        }),
      });
    } catch (error) {
      if (error.status !== 503) showToast(error.message);
    }
    renewClientId(selectedWorkspace.workspace_id);
    clearPageSessionId(window.history);
    setPageSessionRestoring(false);
    setActiveSessionId("");
    setMessages([]);
    setPendingInputs([]);
    setEditingTurnId("");
    setChangeActionStates({});
    setPending(null);
    setStatus("idle");
    run.reset();
    setContextUsage({
      used_tokens: 0,
      window_tokens: bootstrap.context_window_tokens || 0,
    });
    setPanel(null);
    showToast("已新建会话");
  }

  async function deleteSession() {
    if (!deleteTarget || busy || !selectedWorkspace) return;
    const sessionId = deleteTarget.session_id;
    setDeletingSessionId(sessionId);
    try {
      await request(token, "/api/sessions/delete", {
        method: "POST",
        body: JSON.stringify({
          client_id: clientId,
          workspace_id: selectedWorkspace.workspace_id,
          session_id: sessionId,
        }),
      });
      if (activeSessionId === sessionId) {
        renewClientId(selectedWorkspace.workspace_id);
        clearPageSessionId(window.history);
        setPageSessionRestoring(false);
        setActiveSessionId("");
        setMessages([]);
        setPending(null);
        setStatus("idle");
      }
      setDeleteTarget(null);
      await refreshSessions();
      showToast("历史会话已删除");
    } catch (error) {
      showToast(error.message);
    } finally {
      setDeletingSessionId("");
    }
  }

  async function openInfo(kind) {
    if (!selectedWorkspace) return;
    try {
      const suffix = `workspace_id=${encodeURIComponent(selectedWorkspace.workspace_id)}`;
      const path = kind === "diagnostics"
        ? `/api/diagnostics?${suffix}`
        : `/api/${kind}/${clientId}?${suffix}`;
      const data = await request(token, path);
      setPanelContent(
        typeof data.summary === "string" ? data.summary : JSON.stringify(data.summary, null, 2),
      );
      setPanel(kind);
    } catch (error) {
      showToast(error.message);
    }
  }

  if (authState === "checking" && token) {
    return (
      <div className="splash-screen">
        <span className="logo-mark"><Code2 size={23} /></span>
        <RefreshCw className="spin" size={22} />
        <p>正在连接本机 Agent…</p>
      </div>
    );
  }

  if (authState !== "ready") {
    return <LoginView onLogin={login} error={authError} busy={authState === "checking"} />;
  }

  return (
    <div className="app-frame">
      <Sidebar
        mobileOpen={mobileNavOpen}
        selectedWorkspace={selectedWorkspace}
        sessionsCount={sessions.length}
        gitState={gitState}
        runnerOnline={runnerOnline}
        onCloseMobile={() => setMobileNavOpen(false)}
        onOpenProjects={openProjectPicker}
        onOpenInfo={openInfo}
        onOpenSessions={openSessionsPanel}
        onOpenFiles={openFilePanel}
        onOpenGit={openGit}
        onLogout={() => logout()}
      />

      {mobileNavOpen && (
        <button
          className="mobile-backdrop"
          type="button"
          aria-label="关闭菜单"
          onClick={() => setMobileNavOpen(false)}
        />
      )}

      <main className="workspace-main">
        <WorkspaceHeader
          selectedWorkspace={selectedWorkspace}
          sessionRestoring={pageSessionRestoring}
          runnerOnline={runnerOnline}
          model={bootstrap.model}
          contextUsage={contextUsage}
          onOpenMobile={() => setMobileNavOpen(true)}
        />
        <ConversationPane
          selectedWorkspace={selectedWorkspace}
          sessionRestoring={pageSessionRestoring}
          turns={conversationTurns}
          messages={messages}
          busy={busy}
          liveWork={liveWork}
          streamText={streamText}
          runStartedAt={runStartedAt}
          runStage={runStage}
          pending={pending}
          approvalBusyIds={approvalBusyIds}
          messageEndRef={messageEndRef}
          downloadingFileId={downloadingFileId}
          editableTurnId={editableTurnId}
          editingTurnId={editingTurnId}
          editDraft={editDraft}
          editBusy={editBusy}
          changeActionStates={changeActionStates}
          onOpenProjects={openProjectPicker}
          onSubmit={submitPrompt}
          onFileAction={handleOutputFile}
          onOpenChanges={(path) => openFilePanel("changed", path)}
          onBeginEdit={beginEditTurn}
          onEditDraft={setEditDraft}
          onCancelEdit={() => setEditingTurnId("")}
          onSaveEdit={saveEditedTurn}
          onChangeAction={changeTurnFiles}
          onResolveApproval={resolveApproval}
        />
        <Composer
          selectedWorkspace={selectedWorkspace}
          runnerOnline={runnerOnline}
          busy={busy}
          activeTurnId={activeTurnId}
          prompt={prompt}
          attachments={attachments}
          pendingInputs={pendingInputs}
          deliveryMode={deliveryMode}
          permissionPreset={permissionPreset}
          fileInputRef={fileInputRef}
          promptInputRef={promptInputRef}
          onPromptChange={setPrompt}
          onSubmit={() => submitPrompt()}
          onDeliveryModeChange={setDeliveryMode}
          onPermissionPresetChange={changePermissionPreset}
          onAddAttachments={addAttachments}
          onRemoveAttachment={removeAttachment}
        />
      </main>

      <ProjectPicker
        open={projectPickerOpen}
        workspaces={bootstrap.workspaces}
        onSelect={selectWorkspace}
        onClose={closeProjectPicker}
        onRefresh={openProjectPicker}
        required={!selectedWorkspace}
      />

      <FilePanel
        open={filePanelOpen}
        mode={filePanelMode}
        changedFiles={gitState?.changes || []}
        files={projectFiles}
        selectedPath={selectedFilePath}
        diff={gitDiff}
        file={selectedFile}
        loading={filePanelLoading || gitLoading}
        truncated={projectFilesTruncated}
        onClose={closeFilePanel}
        onModeChange={changeFilePanelMode}
        onSelectChanged={selectChangedFile}
        onSelectFile={selectProjectFile}
        onOpenGit={() => {
          closeFilePanel();
          openGit("changes");
        }}
      />

      <GitPanel
        open={gitOpen}
        state={gitState}
        diff={gitDiff}
        view={gitView}
        loading={gitLoading}
        busy={gitBusy || busy}
        onClose={closeGit}
        onRefresh={async () => {
          const latest = await refreshGit();
          if (latest?.available) {
            await loadGitDiff(
              gitView,
              "",
              gitView === "compare"
                ? gitDiff?.base || latest.default_base
                : "",
            );
          }
        }}
        onChangeView={handleGitView}
        onLoadDiff={loadGitDiff}
        onAction={handleGitAction}
        onReview={launchGitReview}
        onLineFeedback={addDiffLineToPrompt}
      />

      <SessionPanel
        open={panel === "sessions"}
        sessions={sessions}
        loading={sessionsLoading}
        busy={busy}
        activeSessionId={activeSessionId}
        resumingSessionId={resumingSessionId}
        deletingSessionId={deletingSessionId}
        onClose={() => setPanel(null)}
        onNewSession={newSession}
        onResume={resumeSession}
        onDeleteRequest={setDeleteTarget}
      />
      <InfoPanel panel={panel} content={panelContent} onClose={() => setPanel(null)} />
      <DeleteSessionDialog
        target={deleteTarget}
        deletingSessionId={deletingSessionId}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={deleteSession}
      />

      {toast && <div className="toast"><Check size={16} /> {toast}</div>}
    </div>
  );
}
