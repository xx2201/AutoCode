const ACTIVE_SESSION_STATE_KEY = "autocodeActiveSession";

export function readPageSessionId(history, workspaceId) {
  const activeSession = history.state?.[ACTIVE_SESSION_STATE_KEY];
  if (!activeSession || activeSession.workspaceId !== workspaceId) return "";
  return typeof activeSession.sessionId === "string" ? activeSession.sessionId : "";
}

export function storePageSessionId(history, workspaceId, sessionId) {
  if (!workspaceId || !sessionId) return;
  history.replaceState(
    {
      ...(history.state || {}),
      [ACTIVE_SESSION_STATE_KEY]: { workspaceId, sessionId },
    },
    "",
  );
}

export function clearPageSessionId(history) {
  if (!history.state?.[ACTIVE_SESSION_STATE_KEY]) return;
  const nextState = { ...history.state };
  delete nextState[ACTIVE_SESSION_STATE_KEY];
  history.replaceState(nextState, "");
}

export function createSessionRequestCoordinator(initialWorkspaceId = "") {
  let selectedWorkspaceId = initialWorkspaceId;
  let sequence = 0;
  let currentRequestId = 0;

  return {
    selectWorkspace(workspaceId) {
      selectedWorkspaceId = workspaceId;
      currentRequestId = ++sequence;
    },

    begin(workspaceId) {
      const requestId = ++sequence;
      if (workspaceId === selectedWorkspaceId) currentRequestId = requestId;
      return { requestId, workspaceId };
    },

    isCurrent(ticket) {
      return ticket.workspaceId === selectedWorkspaceId
        && ticket.requestId === currentRequestId;
    },
  };
}
