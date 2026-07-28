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
  assert.ok(transformed.code.includes("/api/approval/decision"));
  assert.ok(transformed.code.includes("/api/turn/continue/stream"));
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

test("approval batch keeps decisions independent from turn continuation", async () => {
  const source = await readFile(appPath, "utf8");

  assert.match(source, /pending_approvals\.filter/);
  assert.match(source, /approvalBusyIds\[approval\.approval_id\]/);
  assert.match(source, /async function resolveApproval\(approval, action\)/);
  assert.match(source, /async function continueApprovalBatch\(batch\)/);
  const resolver = source.slice(source.indexOf("async function resolveApproval"));
  assert.ok(
    resolver.indexOf("/api/approval/decision")
      < resolver.indexOf("await continueApprovalBatch(batch)"),
  );
});

test("workspace initialization leaves history selection to the user", async () => {
  const source = await readFile(appPath, "utf8");
  const initializeStart = source.indexOf("async function initializeWorkspace");
  const initializeEnd = source.indexOf("initializeWorkspace();", initializeStart);
  const initializeWorkspace = source.slice(initializeStart, initializeEnd);

  assert.ok(initializeStart >= 0, "expected explicit workspace initialization");
  assert.doesNotMatch(source, /SESSION_MAP_KEY|storedSessionId|storeSessionId/);
  assert.doesNotMatch(initializeWorkspace, /\/api\/resume/);
  assert.match(initializeWorkspace, /setActiveSessionId\(""\)/);
  assert.match(initializeWorkspace, /setSessions\(\[\]\)/);
  assert.match(initializeWorkspace, /renewClientId\(selectedWorkspace\.workspace_id\)/);
  assert.match(source, /sessionsLoading/);
  assert.match(source, /sessionRequestsRef\.current\.isCurrent\(requestTicket\)/);
  assert.match(source, /async function openSessionsPanel\(\)/);
  assert.match(source, /setPanel\("sessions"\).*await refreshSessions\(\)/s);
});

test("elapsed timer is isolated from the conversation app state", async () => {
  const source = await readFile(appPath, "utf8");

  assert.match(source, /function LiveElapsed\(\{ startedAt \}\)/);
  assert.doesNotMatch(source, /runElapsedMs|setRunElapsedMs/);
});
