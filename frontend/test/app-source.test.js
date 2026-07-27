import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { transformWithEsbuild } from "vite";

const appPath = new URL("../src/App.jsx", import.meta.url);

test("App JSX compiles after adding interaction controls", async () => {
  const source = await readFile(appPath, "utf8");
  const transformed = await transformWithEsbuild(source, appPath.pathname, {
    loader: "jsx",
    jsx: "automatic",
  });

  assert.ok(transformed.code.includes("/api/turn/edit/stream"));
  assert.ok(transformed.code.includes("/api/turn/message"));
  assert.ok(transformed.code.includes("/api/changes/action"));
  assert.ok(transformed.code.includes("/api/approval/stream"));
  assert.ok(transformed.code.includes("/api/permission-mode"));
  assert.ok(transformed.code.includes("full_access"));
});

test("approval request is rendered in the conversation instead of a page-wide banner", async () => {
  const source = await readFile(appPath, "utf8");
  const approvalIndex = source.indexOf("<ApprovalRequest");
  const messageEndIndex = source.indexOf("<div ref={messageEndRef}");

  assert.ok(approvalIndex >= 0, "expected the approval request component");
  assert.ok(messageEndIndex > approvalIndex, "approval should appear at the end of the conversation");
  assert.doesNotMatch(source, /className="approval-banner"/);
});
