import assert from "node:assert/strict";
import test from "node:test";

import {
  initialRunState,
  runStateReducer,
} from "../src/features/agent-run/run-state.js";

test("run reducer starts and resets one complete agent run", () => {
  const started = runStateReducer(initialRunState, {
    type: "begin",
    startedAt: 123,
    turnId: "turn-1",
  });

  assert.deepEqual(started, {
    streamText: "",
    stage: "queued",
    work: [],
    startedAt: 123,
    activeTurnId: "turn-1",
  });
  assert.deepEqual(runStateReducer(started, { type: "reset" }), initialRunState);
});

test("run reducer preserves model order while accepting narrative and tools", () => {
  let state = runStateReducer(initialRunState, {
    type: "append_token",
    text: "先检查",
  });
  state = runStateReducer(state, {
    type: "accept_work",
    event: {
      type: "work",
      phase: "narrative",
      content: "先检查",
      sequence: 1,
    },
  });
  state = runStateReducer(state, {
    type: "accept_work",
    event: {
      type: "work",
      phase: "started",
      tool_call_id: "call-1",
      tool_name: "shell_command",
      arguments: { command: "pwd" },
      sequence: 2,
    },
  });

  assert.equal(state.streamText, "");
  assert.deepEqual(state.work.map((item) => item.type), ["narrative", "tool"]);
});

test("run reducer tombstones provisional text before a model-step retry", () => {
  const provisional = runStateReducer(initialRunState, {
    type: "append_token",
    text: "partial response",
  });
  const rolledBack = runStateReducer(provisional, { type: "clear_stream" });
  const retried = runStateReducer(rolledBack, {
    type: "append_token",
    text: "replacement response",
  });

  assert.equal(rolledBack.streamText, "");
  assert.equal(retried.streamText, "replacement response");
});

test("run reducer records consumed steer guidance in the live timeline", () => {
  const answered = runStateReducer(initialRunState, {
    type: "append_token",
    text: "current answer",
  });
  const guided = runStateReducer(answered, {
    type: "accept_work",
    event: {
      phase: "guidance",
      work_id: "steer-1",
      content: "focus on tests",
    },
  });

  assert.equal(guided.streamText, "");
  assert.deepEqual(guided.work, [
    {
      id: "steer-1-preceding-response",
      type: "narrative",
      content: "current answer",
    },
    {
      id: "steer-1",
      type: "guidance",
      content: "focus on tests",
    },
  ]);
});
