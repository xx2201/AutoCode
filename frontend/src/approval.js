const KNOWN_REASONS = new Map([
  ["fetching content from an external website", "将访问工作区之外的网站"],
  ["deleting files modifies the workspace", "将删除工作区中的文件"],
  ["external MCP tool call", "将调用外部工具"],
]);

function textValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

function readableToolName(toolName) {
  return textValue(toolName)
    .replace(/^mcp_/, "")
    .replaceAll("_", " ")
    .trim();
}

function externalHost(rawUrl) {
  try {
    return new URL(rawUrl).hostname;
  } catch {
    return "";
  }
}

function detailText(argumentsValue) {
  try {
    return JSON.stringify(argumentsValue || {}, null, 2);
  } catch {
    return String(argumentsValue || "");
  }
}

export function approvalPresentation(pending) {
  const tool = textValue(pending?.pending_tool || pending?.tool_name);
  const argumentsValue = pending?.pending_arguments || pending?.arguments || {};
  const reason = textValue(pending?.pending_reason || pending?.reason);
  const reasonLabel = KNOWN_REASONS.get(reason) || reason || "此操作需要你的许可";
  const scopeLabel = textValue(
    pending?.pending_approval_label || pending?.approval_label,
  );
  const base = {
    tool,
    toolLabel: readableToolName(tool) || "工具操作",
    reason: reasonLabel,
    detail: detailText(argumentsValue),
    detailLabel: "查看请求详情",
    note: scopeLabel
      ? `你可以只允许这一次，或选择“${scopeLabel}”。`
      : "此操作只能单独确认。",
    allowLabel: "允许一次",
    allowAllLabel: scopeLabel ? "本任务允许" : "本任务允许同类操作",
    tone: "default",
    targetLabel: "操作",
    target: readableToolName(tool) || "执行工具",
  };

  if (tool === "web_fetch") {
    const rawUrl = textValue(argumentsValue.url);
    const host = externalHost(rawUrl);
    return {
      ...base,
      title: host ? `允许访问 ${host}？` : "允许访问外部网站？",
      summary: "AutoCode 想读取外部网页内容。网页可能包含不受信任的信息。",
      targetLabel: "网站",
      target: host || rawUrl || "外部网站",
      tone: "network",
    };
  }

  if (tool === "delete_path") {
    const path = textValue(argumentsValue.path);
    return {
      ...base,
      title: "允许删除这个文件？",
      summary: "删除会修改工作区；拒绝后 AutoCode 会收到明确的拒绝结果。",
      targetLabel: "路径",
      target: path || "未提供路径",
      tone: "danger",
    };
  }

  if (tool === "bash") {
    const command = textValue(argumentsValue.command);
    return {
      ...base,
      title: "允许运行这条命令？",
      summary: "命令将由你电脑上的 Runner 执行。",
      targetLabel: "命令",
      target: command || "未提供命令",
      tone: "command",
    };
  }

  if (tool.startsWith("mcp_")) {
    return {
      ...base,
      title: `允许使用 ${base.toolLabel}？`,
      summary: "这个操作由外部 MCP 工具执行，可能访问当前工作区之外的服务。",
      targetLabel: "工具",
      target: base.toolLabel,
      tone: "external",
    };
  }

  return {
    ...base,
    title: "允许 AutoCode 执行此操作？",
    summary: "AutoCode 已暂停，等待你确认后再继续。",
  };
}
