export async function request(token, path, options = {}) {
  const {
    timeoutMs = 0,
    timeoutMessage = "请求超时，请稍后重试。",
    ...fetchOptions
  } = options;
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const controller = timeoutMs > 0 ? new AbortController() : null;
  const timeoutId = controller
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : null;
  try {
    const response = await fetch(path, {
      ...fetchOptions,
      headers,
      signal: controller?.signal || fetchOptions.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.detail || `请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeoutError = new Error(timeoutMessage);
      timeoutError.status = 408;
      throw timeoutError;
    }
    throw error;
  } finally {
    if (timeoutId !== null) window.clearTimeout(timeoutId);
  }
}

export function requestSessionResume(token, {
  clientId,
  workspaceId,
  sessionId,
}) {
  return request(token, "/api/resume", {
    method: "POST",
    timeoutMs: 20_000,
    timeoutMessage: "恢复会话超时；本机任务可能仍在运行，请稍后重试。",
    body: JSON.stringify({
      client_id: clientId,
      workspace_id: workspaceId,
      session_id: sessionId,
    }),
  });
}

export function isTransientRestoreError(error) {
  return !error.status || [408, 409, 429, 502, 503, 504].includes(error.status);
}

export async function streamRequest(token, path, payload, onEvent) {
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  if (!response.body) throw new Error("浏览器不支持流式响应。");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const dataLine = frame
        .split("\n")
        .find((line) => line.startsWith("data: "));
      if (dataLine) onEvent(JSON.parse(dataLine.slice(6)));
    }
    if (done) break;
  }
}

export async function encodeAttachment(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return {
    name: file.name,
    media_type: file.type || "application/octet-stream",
    data_base64: window.btoa(binary),
  };
}
