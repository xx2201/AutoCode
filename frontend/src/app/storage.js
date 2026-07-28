export const TOKEN_KEY = "autocode_web_token";
export const WORKSPACE_KEY = "autocode_workspace_id";
export const PERMISSION_MODE_KEY = "autocode_permission_mode";

const CLIENT_MAP_KEY = "autocode_workspace_clients";

function createClientId() {
  if (window.crypto?.randomUUID) {
    return `web_${window.crypto.randomUUID().replaceAll("-", "")}`;
  }
  return `web_${Date.now()}_${Math.random().toString(36).slice(2, 14)}`;
}

function readClients() {
  try {
    return JSON.parse(localStorage.getItem(CLIENT_MAP_KEY) || "{}");
  } catch {
    return {};
  }
}

export function clientIdFor(workspaceId) {
  const clients = readClients();
  if (!clients[workspaceId]) {
    clients[workspaceId] = createClientId();
    localStorage.setItem(CLIENT_MAP_KEY, JSON.stringify(clients));
  }
  return clients[workspaceId];
}

export function renewClientId(workspaceId) {
  const clients = readClients();
  clients[workspaceId] = createClientId();
  localStorage.setItem(CLIENT_MAP_KEY, JSON.stringify(clients));
  return clients[workspaceId];
}

export function createInteractionId(prefix) {
  if (window.crypto?.randomUUID) return `${prefix}_${window.crypto.randomUUID()}`;
  return `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}
