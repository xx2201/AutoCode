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
