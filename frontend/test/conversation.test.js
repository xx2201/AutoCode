import assert from "node:assert/strict";
import test from "node:test";

import {
  applyTurnLifecycle,
  consumePendingInput,
  formatDuration,
  formatToolTitle,
  groupConversation,
  latestEditableTurnId,
  createPendingInput,
  normalizeChangeAction,
  settlePendingInput,
  mergeWorkEvent,
} from "../src/conversation.js";

test("groups intermediate assistant and tool messages under one work section", () => {
  const turns = groupConversation([
    {
      role: "user",
      content: "创建文件",
      turn_id: "task_1",
      turn_elapsed_ms: 25100,
      changed_files: [
        { path: "hello.txt", status: "added", additions: 1, deletions: 0 },
      ],
    },
    {
      role: "assistant",
      content: "我会先创建文件。",
      turn_id: "task_1",
      tool_calls: [{ id: "call_1", name: "write_file", arguments: { path: "hello.txt" } }],
    },
    {
      role: "tool",
      content: "Wrote 1 line",
      turn_id: "task_1",
      tool_call_id: "call_1",
      tool_name: "write_file",
      tool_arguments: { path: "hello.txt" },
    },
    { role: "assistant", content: "文件已创建。", turn_id: "task_1" },
  ]);

  assert.equal(turns.length, 1);
  assert.equal(turns[0].elapsedMs, 25100);
  assert.equal(turns[0].work.length, 2);
  assert.equal(turns[0].work[0].type, "narrative");
  assert.equal(turns[0].work[1].toolName, "write_file");
  assert.equal(turns[0].answer.content, "文件已创建。");
  assert.deepEqual(turns[0].changedFiles, [
    { path: "hello.txt", status: "added", additions: 1, deletions: 0 },
  ]);
});

test("merges started and completed events using the tool call id", () => {
  const started = mergeWorkEvent([], {
    type: "work",
    phase: "started",
    tool_call_id: "call_1",
    tool_name: "shell_command",
    arguments: { command: "git status" },
  });
  const completed = mergeWorkEvent(started, {
    type: "work",
    phase: "completed",
    tool_call_id: "call_1",
    tool_name: "shell_command",
    output: "clean",
    duration_ms: 1250,
    success: true,
  });

  assert.equal(completed.length, 1);
  assert.deepEqual(completed[0].arguments, { command: "git status" });
  assert.equal(completed[0].content, "clean");
  assert.equal(completed[0].durationMs, 1250);
});

test("keeps live narrative and planned tools in model order", () => {
  const narrative = mergeWorkEvent([], {
    type: "work",
    phase: "narrative",
    work_id: "step-1-narrative",
    content: "我先检查文件，再删除目标。",
  });
  const planned = mergeWorkEvent(narrative, {
    type: "work",
    phase: "planned",
    tool_call_id: "call_1",
    tool_name: "delete_path",
    arguments: { path: "zjh.txt" },
  });
  const completed = mergeWorkEvent(planned, {
    type: "work",
    phase: "completed",
    tool_call_id: "call_1",
    tool_name: "delete_path",
    arguments: { path: "zjh.txt" },
    output: "Deleted zjh.txt",
    duration_ms: 20,
    success: true,
  });

  assert.deepEqual(completed.map((item) => item.type), ["narrative", "tool"]);
  assert.equal(completed[0].content, "我先检查文件，再删除目标。");
  assert.equal(completed[1].toolName, "delete_path");
  assert.deepEqual(completed[1].arguments, { path: "zjh.txt" });
  assert.equal(completed[1].status, "completed");
});

test("formats compact work durations", () => {
  assert.equal(formatDuration(200), "<1s");
  assert.equal(formatDuration(25000), "25s");
  assert.equal(formatDuration(157000), "2m 37s");
});

test("shows the web search query in the work item title", () => {
  assert.equal(
    formatToolTitle("web_search_local", { query: "latest Python release" }),
    "Searched the web · latest Python release",
  );
});

test("shows shell commands with the command-specific work item title", () => {
  assert.equal(
    formatToolTitle("shell_command", { command: "npm test" }),
    "Ran command · npm test",
  );
});

test("does not render legacy model-only visual context as a user turn", () => {
  const turns = groupConversation([
    { role: "user", content: "这张图是什么？" },
    {
      role: "assistant",
      content: "我来读取图片。",
      tool_calls: [{ id: "read-1", name: "read", arguments: { file_path: "screen.png" } }],
    },
    {
      role: "tool",
      content: "Loaded image for visual inspection",
      tool_call_id: "read-1",
      tool_name: "read",
    },
    { role: "user", content: "Visual content loaded by tools: read.\n[image]" },
    { role: "assistant", content: "图片里是 Apple Gift Card。" },
  ]);

  assert.equal(turns.length, 1);
  assert.equal(turns[0].user.content, "这张图是什么？");
  assert.equal(turns[0].answer.content, "图片里是 Apple Gift Card。");
});

test("exposes only the latest settled turn for editing", () => {
  const turns = [
    { id: "turn-1", user: { content: "one" }, answer: { content: "done" } },
    { id: "turn-2", user: { content: "two" }, answer: null },
  ];

  assert.equal(latestEditableTurnId(turns, false, "failed"), "turn-2");
  assert.equal(latestEditableTurnId(turns, false, "completed"), "turn-2");
  assert.equal(latestEditableTurnId(turns, false, "running"), "");
  assert.equal(latestEditableTurnId(turns, true, "failed"), "");
});

test("tracks queue and steer messages through their request lifecycle", () => {
  const queued = createPendingInput("  run tests next  ", "queue", "input-1");
  assert.deepEqual(queued, {
    id: "input-1",
    prompt: "run tests next",
    mode: "queue",
    status: "sending",
  });
  assert.deepEqual(
    settlePendingInput([queued], "input-1", "accepted", "queued"),
    [{ ...queued, status: "accepted", detail: "queued" }],
  );
});

test("promotes a queued message after preserving the completed turn snapshot", () => {
  const completed = [
    { role: "user", content: "first", turn_id: "turn-1" },
    { role: "assistant", content: "done", turn_id: "turn-1" },
  ];
  const starting = applyTurnLifecycle([], {
    type: "turn",
    phase: "queued_starting",
    message_id: "queue-1",
    content: "second",
    messages: completed,
  });
  const started = applyTurnLifecycle(starting, {
    type: "turn",
    phase: "started",
    queued: true,
    message_id: "queue-1",
    content: "second",
    turn_id: "turn-2",
  });

  assert.deepEqual(started.slice(0, 2), completed);
  assert.deepEqual(started[2], {
    role: "user",
    content: "second",
    message_id: "queue-1",
    message_kind: "prompt",
    turn_id: "turn-2",
  });
  assert.deepEqual(
    consumePendingInput([{ id: "queue-1" }, { id: "queue-2" }], "queue-1"),
    [{ id: "queue-2" }],
  );
});

test("keeps consumed steer input inside the active turn", () => {
  const messages = applyTurnLifecycle(
    [{ role: "user", content: "inspect", turn_id: "turn-1" }],
    {
      type: "turn_message",
      phase: "consumed",
      mode: "steer",
      message_id: "steer-1",
      turn_id: "turn-1",
      content: "focus on tests",
    },
  );
  const turns = groupConversation(messages);

  assert.equal(turns.length, 1);
  assert.equal(turns[0].user.content, "inspect");
  assert.deepEqual(turns[0].work[0], {
    id: "steer-1",
    type: "guidance",
    content: "focus on tests",
  });
});

test("normalizes undo conflicts without losing the server reason", () => {
  assert.deepEqual(normalizeChangeAction({
    status: "conflict",
    conflict: true,
    conflict_reason: "文件已被再次修改",
  }, "undo"), {
    state: "conflict",
    conflict: true,
    detail: "文件已被再次修改",
  });
  assert.deepEqual(normalizeChangeAction({}, "reapply"), {
    state: "applied",
    conflict: false,
    detail: "",
  });
});
