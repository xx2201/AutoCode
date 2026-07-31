export function formatDuration(milliseconds) {
  const seconds = Math.max(0, Math.round(Number(milliseconds || 0) / 1000));
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

export function formatToolTitle(name, argumentsValue = {}) {
  const target = (
    argumentsValue.path
    || argumentsValue.file_path
    || argumentsValue.command
    || argumentsValue.query
    || ""
  );
  const labels = {
    shell_command: "Ran command",
    read: "Read file",
    write_file: "Wrote file",
    edit_file: "Edited file",
    list_files: "Listed files",
    start_process: "Started background process",
    stop_process: "Stopped background process",
    web_search_local: "Searched the web",
    web_fetch: "Fetched URL",
  };
  const label = labels[name] || (name ? `Used ${name.replaceAll("_", " ")}` : "Ran tool");
  if (!target) return label;
  const compact = String(target).replace(/\s+/g, " ").trim();
  return `${label} · ${compact.length > 96 ? `${compact.slice(0, 93)}…` : compact}`;
}

function messageKey(message, index) {
  return (
    message.turn_id
    || message.tool_call_id
    || `${message.role}-${index}-${String(message.content || "").slice(0, 24)}`
  );
}

export function groupConversation(messages) {
  const turns = [];
  let current = null;

  messages.forEach((message, index) => {
    if (
      message.role === "user"
      && String(message.content || "").startsWith("Visual content loaded by tools:")
    ) {
      return;
    }
    if (message.role === "user") {
      current = {
        id: messageKey(message, index),
        user: message,
        work: [],
        answer: null,
        elapsedMs: Number(message.turn_elapsed_ms || 0),
        changedFiles: message.changed_files || [],
      };
      turns.push(current);
      return;
    }

    if (!current) {
      current = {
        id: messageKey(message, index),
        user: null,
        work: [],
        answer: null,
        elapsedMs: Number(message.turn_elapsed_ms || 0),
        changedFiles: [],
      };
      turns.push(current);
    }

    if (message.role === "tool") {
      const completedTool = {
        id: message.tool_call_id || messageKey(message, index),
        type: "tool",
        content: message.content || "",
        toolName: message.tool_name || "",
        arguments: message.tool_arguments || {},
      };
      const pendingIndex = current.work.findIndex(
        (item) => item.type === "pending-tool" && item.id === completedTool.id,
      );
      if (pendingIndex >= 0) {
        current.work[pendingIndex] = completedTool;
      } else {
        current.work.push(completedTool);
      }
      return;
    }

    if (message.role === "assistant") {
      const toolCalls = message.tool_calls || [];
      if (toolCalls.length > 0) {
        if (message.content) {
          current.work.push({
            id: messageKey(message, index),
            type: "narrative",
            content: message.content,
          });
        }
        toolCalls.forEach((toolCall, toolIndex) => {
          const alreadyRecorded = current.work.some(
            (item) => item.type === "tool" && item.id === toolCall.id,
          );
          if (!alreadyRecorded) {
            current.work.push({
              id: toolCall.id || `${messageKey(message, index)}-tool-${toolIndex}`,
              type: "pending-tool",
              content: "",
              toolName: toolCall.name || "",
              arguments: toolCall.arguments || {},
            });
          }
        });
      } else {
        current.answer = message;
      }
    }
  });

  return turns;
}

export function mergeWorkEvent(items, event) {
  if (event.phase === "narrative") {
    const content = String(event.content || "");
    if (!content) return items;
    const id = event.work_id || `narrative-${items.length}`;
    const index = items.findIndex((item) => item.id === id);
    const narrative = { id, type: "narrative", content };
    if (index < 0) return [...items, narrative];
    return items.map((item, itemIndex) => (itemIndex === index ? narrative : item));
  }
  const id = event.tool_call_id || `${event.tool_name || "tool"}-${items.length}`;
  const index = items.findIndex((item) => item.id === id);
  const next = {
    id,
    type: "tool",
    toolName: event.tool_name || "",
    arguments: event.arguments || {},
    content: event.output || "",
    durationMs: Number(event.duration_ms || 0),
    status: event.phase || "started",
    success: event.success !== false,
  };
  if (index < 0) return [...items, next];
  const phaseRank = { planned: 0, started: 1, completed: 2 };
  return items.map((item, itemIndex) => {
    if (itemIndex !== index) return item;
    const advances = (phaseRank[next.status] ?? 0) >= (phaseRank[item.status] ?? 0);
    return {
      ...item,
      ...next,
      toolName: next.toolName || item.toolName,
      arguments: Object.keys(next.arguments).length ? next.arguments : item.arguments,
      content: next.content || item.content,
      durationMs: advances ? next.durationMs : item.durationMs,
      status: advances ? next.status : item.status,
      success: advances ? next.success : item.success,
    };
  });
}

export function latestEditableTurnId(turns, busy = false, status = "") {
  if (busy || !["completed", "failed"].includes(status)) return "";
  const latest = turns.at(-1);
  return latest?.user ? latest.id : "";
}

export function createPendingInput(prompt, mode, id) {
  return {
    id,
    prompt: String(prompt || "").trim(),
    mode: mode === "queue" ? "queue" : "steer",
    status: "sending",
  };
}

export function settlePendingInput(items, id, status, detail = "") {
  return items.map((item) => (
    item.id === id ? { ...item, status, detail } : item
  ));
}

export function normalizeChangeAction(result, action) {
  const state = result.state || result.status || (action === "undo" ? "undone" : "applied");
  return {
    state,
    conflict: Boolean(result.conflict),
    detail: result.conflict_reason || result.detail || result.message || "",
  };
}
