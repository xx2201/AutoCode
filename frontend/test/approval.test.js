import assert from "node:assert/strict";
import test from "node:test";

import { approvalPresentation } from "../src/approval.js";

test("web fetch approval presents the host and hides verbose arguments in details", () => {
  const view = approvalPresentation({
    pending_tool: "web_fetch",
    pending_reason: "fetching content from an external website",
    pending_requires_manual: true,
    pending_arguments: {
      url: "https://commons.wikimedia.org/w/api.php?action=query&very_long=value",
      prompt: "Return the raw URL for every image.",
    },
  });

  assert.equal(view.title, "允许访问 commons.wikimedia.org？");
  assert.equal(view.target, "commons.wikimedia.org");
  assert.equal(view.reason, "将访问工作区之外的网站");
  assert.match(view.detail, /very_long/);
  assert.doesNotMatch(view.summary, /Return the raw URL/);
  assert.match(view.note, /需要单独确认/);
});

test("delete approval names the target without weakening manual confirmation", () => {
  const view = approvalPresentation({
    pending_tool: "delete_path",
    pending_reason: "deleting files modifies the workspace",
    pending_requires_manual: true,
    pending_arguments: { path: "src/old.py" },
  });

  assert.equal(view.title, "允许删除这个文件？");
  assert.equal(view.targetLabel, "路径");
  assert.equal(view.target, "src/old.py");
  assert.equal(view.tone, "danger");
  assert.match(view.note, /不会被本轮自动许可覆盖/);
});

test("ordinary MCP confirmation explains the task-scoped approval option", () => {
  const view = approvalPresentation({
    pending_tool: "mcp_issue_tracker_create",
    pending_reason: "external MCP tool call",
    pending_requires_manual: false,
    pending_arguments: { title: "Fix regression" },
  });

  assert.equal(view.title, "允许使用 issue tracker create？");
  assert.equal(view.allowAllLabel, "本轮不再询问");
  assert.match(view.note, /当前任务/);
});
