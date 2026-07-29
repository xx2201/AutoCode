const ACTIVE_SESSION_STATE_KEY = "autocodeActiveSession";
const SESSION_QUERY_KEY = "session";
const WORKSPACE_QUERY_KEY = "workspace";

function sessionRoute(location, update) {
  if (!location?.href) return "";
  const url = new URL(location.href);
  update(url.searchParams);
  return `${url.pathname}${url.search}${url.hash}`;
}

export function readPageSessionId(
  history,
  workspaceId,
  location = globalThis.location,
) {
  const activeSession = history.state?.[ACTIVE_SESSION_STATE_KEY];
  if (activeSession?.workspaceId === workspaceId) {
    return typeof activeSession.sessionId === "string"
      ? activeSession.sessionId
      : "";
  }
  if (!location?.search) return "";
  const query = new URLSearchParams(location.search);
  if (query.get(WORKSPACE_QUERY_KEY) !== workspaceId) return "";
  return query.get(SESSION_QUERY_KEY) || "";
}

export function storePageSessionId(
  history,
  workspaceId,
  sessionId,
  location = globalThis.location,
) {
  if (!workspaceId || !sessionId) return;
  const state = {
    ...(history.state || {}),
    [ACTIVE_SESSION_STATE_KEY]: { workspaceId, sessionId },
  };
  const route = sessionRoute(
    location,
    (query) => {
      query.set(WORKSPACE_QUERY_KEY, workspaceId);
      query.set(SESSION_QUERY_KEY, sessionId);
    },
  );
  history.replaceState(state, "", route || undefined);
}

export function clearPageSessionId(history, location = globalThis.location) {
  const hasState = Boolean(history.state?.[ACTIVE_SESSION_STATE_KEY]);
  const hasRoute = Boolean(
    location?.search
    && new URLSearchParams(location.search).has(SESSION_QUERY_KEY),
  );
  if (!hasState && !hasRoute) return;
  const nextState = { ...history.state };
  delete nextState[ACTIVE_SESSION_STATE_KEY];
  const route = sessionRoute(
    location,
    (query) => {
      query.delete(WORKSPACE_QUERY_KEY);
      query.delete(SESSION_QUERY_KEY);
    },
  );
  history.replaceState(nextState, "", route || undefined);
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
