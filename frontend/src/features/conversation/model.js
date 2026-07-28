export function attachChangedFilesToLatestTurn(messages, changedFiles = []) {
  if (changedFiles.length === 0) return messages;
  const latestUser = messages.findLastIndex((message) => message.role === "user");
  if (latestUser < 0) return messages;
  const next = [...messages];
  next[latestUser] = {
    ...next[latestUser],
    changed_files: changedFiles,
  };
  return next;
}

export function mergeResultMessages(
  currentMessages,
  synchronizedMessages,
  result,
  changedFiles = result?.changed_files || [],
) {
  if (synchronizedMessages.length > 0) {
    const next = attachChangedFilesToLatestTurn(
      synchronizedMessages,
      changedFiles,
    );
    const lastAssistant = next.findLastIndex(
      (message) => message.role === "assistant" && !(message.tool_calls?.length),
    );
    if (lastAssistant >= 0 && result?.files?.length) {
      next[lastAssistant] = {
        ...next[lastAssistant],
        files: result.files,
      };
    }
    return next;
  }
  if (!result?.text && !result?.files?.length) return currentMessages;
  return [
    ...attachChangedFilesToLatestTurn(currentMessages, changedFiles),
    {
      role: "assistant",
      content: result.text || "文件已准备好。",
      files: result.files || [],
    },
  ];
}
