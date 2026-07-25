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
    bash: "Ran command",
    read_file: "Read file",
    write_file: "Wrote file",
    edit_file: "Edited file",
    list_files: "Listed files",
    read_image: "Viewed image",
    start_process: "Started background process",
    stop_process: "Stopped background process",
    web_search: "Searched the web",
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
  return items.map((item, itemIndex) => (
    itemIndex === index
      ? {
          ...item,
          ...next,
          arguments: Object.keys(next.arguments).length ? next.arguments : item.arguments,
        }
      : item
  ));
}
