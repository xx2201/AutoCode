import assert from "node:assert/strict";
import test from "node:test";

import {
  formatDuration,
  groupConversation,
  mergeWorkEvent,
} from "../src/conversation.js";

test("groups intermediate assistant and tool messages under one work section", () => {
  const turns = groupConversation([
    { role: "user", content: "创建文件", turn_id: "task_1", turn_elapsed_ms: 25100 },
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
