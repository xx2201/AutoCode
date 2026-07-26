import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDuration,
  formatToolTitle,
  groupConversation,
  latestCompletedTurnId,
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
    tool_name: "bash",
    arguments: { command: "git status" },
  });
  const completed = mergeWorkEvent(started, {
    type: "work",
    phase: "completed",
    tool_call_id: "call_1",
    tool_name: "bash",
    output: "clean",
    duration_ms: 1250,
    success: true,
  });

  assert.equal(completed.length, 1);
  assert.deepEqual(completed[0].arguments, { command: "git status" });
  assert.equal(completed[0].content, "clean");
  assert.equal(completed[0].durationMs, 1250);
});

test("formats compact work durations", () => {
  assert.equal(formatDuration(200), "<1s");
  assert.equal(formatDuration(25000), "25s");
  assert.equal(formatDuration(157000), "2m 37s");
});

test("shows the web search query in the work item title", () => {
  assert.equal(
    formatToolTitle("web_search", { query: "latest Python release" }),
    "Searched the web · latest Python release",
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

test("only exposes the latest completed turn for editing", () => {
  const turns = [
    { id: "turn-1", user: { content: "one" }, answer: { content: "done" } },
    { id: "turn-2", user: { content: "two" }, answer: null },
  ];

  assert.equal(latestCompletedTurnId(turns), "turn-1");
  assert.equal(latestCompletedTurnId(turns, true), "");
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
