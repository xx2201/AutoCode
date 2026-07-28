import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { transformWithEsbuild } from "vite";

const sourcePaths = {
  app: new URL("../src/App.jsx", import.meta.url),
  approval: new URL(
    "../src/features/approvals/ApprovalRequest.jsx",
    import.meta.url,
  ),
  composer: new URL(
    "../src/features/conversation/Composer.jsx",
    import.meta.url,
  ),
  conversation: new URL(
    "../src/features/conversation/ConversationView.jsx",
    import.meta.url,
  ),
  conversationPane: new URL(
    "../src/features/conversation/ConversationPane.jsx",
    import.meta.url,
  ),
  sessions: new URL(
    "../src/features/sessions/SessionPanel.jsx",
    import.meta.url,
  ),
};

async function readSources() {
  return Object.fromEntries(await Promise.all(
    Object.entries(sourcePaths).map(async ([name, path]) => [
      name,
      await readFile(path, "utf8"),
    ]),
  ));
}

test("frontend architecture JSX modules compile independently", async () => {
  for (const path of Object.values(sourcePaths)) {
    const source = await readFile(path, "utf8");
    const transformed = await transformWithEsbuild(source, path.pathname, {
      loader: "jsx",
      jsx: "automatic",
    });
    assert.ok(transformed.code.length > 0, `expected ${path.pathname} to compile`);
  }
});

test("App orchestrates domain modules instead of declaring UI subsystems", async () => {
  const { app } = await readSources();

  assert.match(app, /useWorkspaceBootstrap/);
  assert.match(app, /useWorkspaceFiles/);
  assert.match(app, /useAgentRun/);
  assert.match(app, /<ConversationPane/);
  assert.match(app, /<Composer/);
  assert.doesNotMatch(
    app,
    /function (ApprovalRequest|ProjectPicker|ConversationTurn|WorkBlock|ContextMeter)/,
  );
  assert.ok(app.split(/\r?\n/).length < 1200, "App should remain an orchestration layer");
});

test("agent interaction endpoints remain wired after the architecture split", async () => {
  const { app } = await readSources();

  assert.match(app, /\/api\/turn\/edit\/stream/);
  assert.match(app, /\/api\/turn\/message/);
  assert.match(app, /\/api\/changes\/action/);
  assert.match(app, /\/api\/approval\/decision/);
  assert.match(app, /\/api\/turn\/continue\/stream/);
  assert.match(app, /\/api\/permission-mode/);
  assert.match(app, /full_access/);
});

test("approval request is rendered in the conversation instead of a page banner", async () => {
  const { conversationPane } = await readSources();
  const approvalIndex = conversationPane.indexOf("<ApprovalRequest");
  const messageEndIndex = conversationPane.indexOf("<div ref={messageEndRef}");

  assert.ok(approvalIndex >= 0, "expected the approval request component");
  assert.ok(messageEndIndex > approvalIndex, "approval should follow the conversation");
  assert.doesNotMatch(conversationPane, /className="approval-banner"/);
});

test("approval batch keeps decisions independent from turn continuation", async () => {
  const { app } = await readSources();

  assert.match(app, /async function resolveApproval\(approval, action\)/);
  assert.match(app, /async function continueApprovalBatch\(batch\)/);
  const resolver = app.slice(app.indexOf("async function resolveApproval"));
  assert.ok(
    resolver.indexOf("/api/approval/decision")
      < resolver.indexOf("await continueApprovalBatch(batch)"),
  );
});

test("workspace initialization restores only a session from the current page", async () => {
  const { app } = await readSources();
  const initializeStart = app.indexOf("async function initializeWorkspace");
  const initializeEnd = app.indexOf("initializeWorkspace();", initializeStart);
  const initializeWorkspace = app.slice(initializeStart, initializeEnd);

  assert.ok(initializeStart >= 0, "expected explicit workspace initialization");
  assert.doesNotMatch(app, /SESSION_MAP_KEY|storedSessionId|storeSessionId/);
  assert.match(app, /readPageSessionId/);
  assert.match(app, /storePageSessionId/);
  assert.match(app, /clearPageSessionId/);
  assert.match(initializeWorkspace, /requestSessionResume/);
  assert.match(initializeWorkspace, /availableSessions\.some/);
  assert.match(initializeWorkspace, /renewClientId\(workspaceId\)/);
});

test("page session restoration renders loading before the welcome screen", async () => {
  const { app, conversationPane } = await readSources();
  const restoreBranch = conversationPane.indexOf("sessionRestoring ? (");
  const welcomeBranch = conversationPane.indexOf("messages.length === 0 ? (");
  const initializeStart = app.indexOf("async function initializeWorkspace");
  const initializeEnd = app.indexOf("initializeWorkspace();", initializeStart);
  const initializeWorkspace = app.slice(initializeStart, initializeEnd);

  assert.ok(restoreBranch >= 0, "expected a page-session restoring branch");
  assert.ok(restoreBranch < welcomeBranch, "restore loading must precede welcome");
  assert.match(initializeWorkspace, /setPageSessionRestoring\(Boolean\(pageSessionId\)\)/);
  assert.match(conversationPane, /正在恢复历史会话/);
});

test("elapsed timer and live narrative ordering live in the conversation feature", async () => {
  const { conversation } = await readSources();
  const workBlockStart = conversation.indexOf("export function WorkBlock");
  const workBlockEnd = conversation.indexOf("function TurnChangedFiles", workBlockStart);
  const workBlock = conversation.slice(workBlockStart, workBlockEnd);

  assert.match(conversation, /function LiveElapsed\(\{ startedAt \}\)/);
  assert.doesNotMatch(conversation, /runElapsedMs|setRunElapsedMs/);
  assert.ok(workBlock.indexOf("{items.map") >= 0);
  assert.ok(workBlock.indexOf("{liveText &&") > workBlock.indexOf("{items.map"));
});
