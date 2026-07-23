const state = {
  token: localStorage.getItem("autocode_web_token") || "",
  clientId: localStorage.getItem("autocode_client_id") || createClientId(),
  busy: false,
  pending: null,
  runnerOnline: false,
};

const $ = (selector) => document.querySelector(selector);
const loginView = $("#login-view");
const appView = $("#app-view");
const loginForm = $("#login-form");
const tokenInput = $("#token-input");
const loginError = $("#login-error");
const messageList = $("#message-list");
const welcomeCard = $("#welcome-card");
const composerForm = $("#composer-form");
const promptInput = $("#prompt-input");
const sendButton = $("#send-button");
const approvalCard = $("#approval-card");
const statusBadge = $("#status-badge");
const connectionLabel = $("#connection-label");
let toastTimer;

localStorage.setItem("autocode_client_id", state.clientId);

function createClientId() {
  if (window.crypto?.randomUUID) {
    return `web_${window.crypto.randomUUID().replaceAll("-", "")}`;
  }
  return `web_${Date.now()}_${Math.random().toString(36).slice(2, 14)}`;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function showApp() {
  loginView.classList.add("hidden");
  appView.classList.remove("hidden");
  promptInput.focus();
}

function showLogin(message = "") {
  appView.classList.add("hidden");
  loginView.classList.remove("hidden");
  loginError.textContent = message;
  tokenInput.value = "";
  tokenInput.focus();
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.add("hidden"), 2600);
}

function setBusy(busy) {
  state.busy = busy;
  sendButton.disabled = busy || !state.runnerOnline;
  promptInput.disabled = busy || !state.runnerOnline;
  statusBadge.textContent = busy ? "运行中" : (state.pending ? "等待确认" : "空闲");
  statusBadge.className = `status-badge ${busy ? "running" : (state.pending ? "waiting" : "idle")}`;
}

function setRunnerConnection(online) {
  state.runnerOnline = online;
  connectionLabel.classList.toggle("offline", !online);
  connectionLabel.innerHTML = `<i></i> ${online ? "本机 Runner 已连接" : "本机 Runner 离线"}`;
  if (!state.busy) {
    sendButton.disabled = !online;
    promptInput.disabled = !online;
  }
}

function updateStatus(result) {
  const labels = {
    running: "运行中",
    waiting_approval: "等待确认",
    completed: "已完成",
    failed: "失败",
  };
  const value = result?.status || "idle";
  statusBadge.textContent = labels[value] || value;
  statusBadge.className = `status-badge ${
    value === "waiting_approval" ? "waiting" : value
  }`;
  state.pending = result?.pending_tool ? result : null;
  renderApproval();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderMarkdown(source) {
  const escaped = escapeHtml(source || "");
  const blocks = escaped.split(/```/);
  const rendered = blocks.map((block, index) => {
    if (index % 2 === 1) return `<pre>${block.replace(/^[a-zA-Z0-9_+-]+\n/, "")}</pre>`;
    return block
      .replace(/^### (.+)$/gm, "<h4>$1</h4>")
      .replace(/^## (.+)$/gm, "<h3>$1</h3>")
      .replace(/^# (.+)$/gm, "<h2>$1</h2>")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .split(/\n{2,}/)
      .map((paragraph) => `<p>${paragraph.replace(/\n/g, "<br>")}</p>`)
      .join("");
  });
  return rendered.join("");
}

function removeWelcome() {
  if (welcomeCard) welcomeCard.remove();
}

function appendMessage(role, content) {
  if (!content) return;
  removeWelcome();
  const article = document.createElement("article");
  article.className = `message ${role}`;
  const labels = { user: "YOU", assistant: "AUTOCODE", tool: "TOOL OUTPUT" };
  const icons = { user: "Y", assistant: "AC", tool: "›_" };
  article.innerHTML = `
    <div class="message-meta"><span>${icons[role] || "•"}</span>${labels[role] || role}</div>
    <div class="message-body">${role === "tool" ? `<pre>${escapeHtml(content)}</pre>` : renderMarkdown(content)}</div>`;
  messageList.appendChild(article);
  messageList.scrollTop = messageList.scrollHeight;
}

function showTyping() {
  removeWelcome();
  const article = document.createElement("article");
  article.id = "typing-message";
  article.className = "message assistant";
  article.innerHTML = `
    <div class="message-meta"><span>AC</span>AUTOCODE</div>
    <div class="typing"><i></i><i></i><i></i></div>`;
  messageList.appendChild(article);
  messageList.scrollTop = messageList.scrollHeight;
}

function hideTyping() {
  $("#typing-message")?.remove();
}

function renderApproval() {
  if (!state.pending) {
    approvalCard.classList.add("hidden");
    return;
  }
  $("#approval-reason").textContent = `${state.pending.pending_tool} · ${state.pending.pending_reason || "需要确认"}`;
  const command = state.pending.pending_arguments?.command || JSON.stringify(state.pending.pending_arguments || {}, null, 2);
  $("#approval-command").textContent = command;
  $("#approve-all-button").classList.toggle("hidden", Boolean(state.pending.pending_requires_manual));
  approvalCard.classList.remove("hidden");
}

async function loadBootstrap() {
  const data = await api("/api/bootstrap");
  setRunnerConnection(true);
  $("#workspace-badge").textContent = `⌁ ${data.workspace}`;
  $("#model-badge").textContent = `◇ ${data.model}`;
  renderSessions(data.sessions);
}

function showRunnerOffline(message = "本机 Runner 未连接，请确认电脑已开机。") {
  setRunnerConnection(false);
  $("#workspace-badge").textContent = "⌁ 本机工作区";
  $("#model-badge").textContent = "◇ 等待 Runner";
  renderSessions([]);
  toast(message);
}

async function enterWorkspace() {
  showApp();
  try {
    await loadBootstrap();
  } catch (error) {
    if (error.status === 401) {
      logout("访问令牌已失效。");
      return;
    }
    showRunnerOffline(error.message);
  }
}

async function submitPrompt(prompt) {
  if (!state.runnerOnline) {
    toast("本机 Runner 离线，暂时不能执行任务。");
    return;
  }
  if (state.busy || !prompt.trim()) return;
  appendMessage("user", prompt.trim());
  promptInput.value = "";
  autoResize();
  setBusy(true);
  showTyping();
  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ client_id: state.clientId, prompt: prompt.trim() }),
    });
    hideTyping();
    appendMessage("assistant", result.text);
    updateStatus(result);
    await refreshSessions();
  } catch (error) {
    hideTyping();
    appendMessage("assistant", `请求失败：${error.message}`);
    if (error.status === 401) logout("访问令牌已失效。");
  } finally {
    setBusy(false);
  }
}

async function resolveApproval(action) {
  if (!state.pending || state.busy) return;
  setBusy(true);
  showTyping();
  try {
    const result = await api("/api/approval", {
      method: "POST",
      body: JSON.stringify({ client_id: state.clientId, action }),
    });
    hideTyping();
    appendMessage("assistant", result.text);
    updateStatus(result);
    await refreshSessions();
  } catch (error) {
    hideTyping();
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

function renderSessions(sessions) {
  const container = $("#session-list");
  container.replaceChildren();
  if (!sessions?.length) {
    const empty = document.createElement("p");
    empty.className = "login-copy";
    empty.textContent = "还没有可恢复的会话。";
    container.appendChild(empty);
    return;
  }
  sessions.forEach((session) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-item";
    const title = document.createElement("strong");
    title.textContent = session.title || session.session_id;
    const meta = document.createElement("small");
    meta.textContent = `${session.status} · step ${session.step_index} · ${session.saved_at}`;
    button.append(title, meta);
    button.addEventListener("click", () => resumeSession(session.session_id));
    container.appendChild(button);
  });
}

async function refreshSessions() {
  const data = await api("/api/sessions");
  renderSessions(data.sessions);
}

async function resumeSession(sessionId) {
  closeDrawer();
  setBusy(true);
  try {
    const data = await api("/api/resume", {
      method: "POST",
      body: JSON.stringify({ client_id: state.clientId, session_id: sessionId }),
    });
    messageList.replaceChildren();
    data.messages.forEach((message) => appendMessage(message.role, message.content));
    updateStatus(data.result);
    toast("会话已恢复");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function newSession() {
  await api("/api/reset", {
    method: "POST",
    body: JSON.stringify({ client_id: state.clientId }),
  });
  state.clientId = createClientId();
  localStorage.setItem("autocode_client_id", state.clientId);
  state.pending = null;
  window.location.reload();
}

async function showInfo(kind) {
  try {
    const data = await api(`/api/${kind}/${state.clientId}`);
    $("#modal-title").textContent = kind === "trace" ? "运行 Trace" : "当前任务";
    $("#modal-content").textContent = data.summary;
    $("#modal-backdrop").classList.remove("hidden");
  } catch (error) {
    toast(error.message);
  }
}

async function showOverview() {
  try {
    const [task, trace] = await Promise.all([
      api(`/api/task/${state.clientId}`),
      api(`/api/trace/${state.clientId}`),
    ]);
    $("#modal-title").textContent = "任务与 Trace";
    $("#modal-content").textContent = `${task.summary}\n\n──────── TRACE ────────\n\n${trace.summary}`;
    $("#modal-backdrop").classList.remove("hidden");
  } catch (error) {
    toast(error.message);
  }
}

function openDrawer() {
  $("#session-drawer").classList.add("open");
  $("#drawer-backdrop").classList.remove("hidden");
  refreshSessions().catch((error) => toast(error.message));
}

function closeDrawer() {
  $("#session-drawer").classList.remove("open");
  $("#drawer-backdrop").classList.add("hidden");
}

function logout(message = "") {
  localStorage.removeItem("autocode_web_token");
  state.token = "";
  showLogin(message);
}

function autoResize() {
  promptInput.style.height = "auto";
  promptInput.style.height = `${Math.min(promptInput.scrollHeight, 160)}px`;
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const candidate = tokenInput.value.trim();
  if (!candidate) return;
  state.token = candidate;
  loginError.textContent = "";
  try {
    await api("/api/auth/verify", { method: "POST" });
    localStorage.setItem("autocode_web_token", candidate);
    await enterWorkspace();
  } catch (error) {
    state.token = "";
    loginError.textContent = error.message;
  }
});

composerForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitPrompt(promptInput.value);
});
promptInput.addEventListener("input", autoResize);
promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composerForm.requestSubmit();
  }
});
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => submitPrompt(button.dataset.prompt));
});
$("#approve-button").addEventListener("click", () => resolveApproval("approve"));
$("#approve-all-button").addEventListener("click", () => resolveApproval("approve_all"));
$("#reject-button").addEventListener("click", () => resolveApproval("reject"));
$("#sessions-button").addEventListener("click", openDrawer);
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
$("#new-session-button").addEventListener("click", newSession);
$("#logout-button").addEventListener("click", () => logout());
$("#trace-button").addEventListener("click", () => showInfo("trace"));
$("#more-button").addEventListener("click", showOverview);
$("#modal-close").addEventListener("click", () => $("#modal-backdrop").classList.add("hidden"));
$("#modal-backdrop").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.classList.add("hidden");
});

if (state.token) {
  api("/api/auth/verify", { method: "POST" })
    .then(enterWorkspace)
    .catch(() => logout("保存的访问令牌已失效，请重新输入。"));
} else {
  showLogin();
}

setInterval(async () => {
  if (appView.classList.contains("hidden")) return;
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    const health = await response.json();
    if (!health.runner_connected) {
      setRunnerConnection(false);
    } else if (!state.runnerOnline) {
      await loadBootstrap();
      toast("本机 Runner 已重新连接");
    }
  } catch {
    setRunnerConnection(false);
  }
}, 15000);
