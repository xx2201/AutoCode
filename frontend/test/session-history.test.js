import assert from "node:assert/strict";
import test from "node:test";

import {
  clearPageSessionId,
  createSessionRequestCoordinator,
  readPageSessionId,
  storePageSessionId,
} from "../src/session-history.js";

function memoryHistory(initial = null) {
  return {
    state: initial,
    url: "",
    replaceState(nextState, _unused, url = "") {
      this.state = nextState;
      this.url = url;
    },
  };
}

function memoryLocation(search = "", hash = "") {
  return {
    href: `https://example.test/${search}${hash}`,
    pathname: "/",
    search,
    hash,
  };
}

test("switching workspace invalidates an in-flight session request", () => {
  const coordinator = createSessionRequestCoordinator("workspace-a");
  const staleRequest = coordinator.begin("workspace-a");

  coordinator.selectWorkspace("workspace-b");
  const currentRequest = coordinator.begin("workspace-b");

  assert.equal(coordinator.isCurrent(staleRequest), false);
  assert.equal(coordinator.isCurrent(currentRequest), true);
});

test("only the newest session refresh may update a workspace", () => {
  const coordinator = createSessionRequestCoordinator("workspace-a");
  const firstRequest = coordinator.begin("workspace-a");
  const secondRequest = coordinator.begin("workspace-a");

  assert.equal(coordinator.isCurrent(firstRequest), false);
  assert.equal(coordinator.isCurrent(secondRequest), true);
});

test("a late request from the previous workspace cannot invalidate the current refresh", () => {
  const coordinator = createSessionRequestCoordinator("workspace-a");

  coordinator.selectWorkspace("workspace-b");
  const currentRequest = coordinator.begin("workspace-b");
  const latePreviousRequest = coordinator.begin("workspace-a");

  assert.equal(coordinator.isCurrent(latePreviousRequest), false);
  assert.equal(coordinator.isCurrent(currentRequest), true);
});

test("the current page stores the explicitly opened session", () => {
  const history = memoryHistory({ unrelated: "preserved" });

  storePageSessionId(history, "workspace-a", "session-a");

  assert.equal(readPageSessionId(history, "workspace-a"), "session-a");
  assert.equal(readPageSessionId(history, "workspace-b"), "");
  assert.equal(history.state.unrelated, "preserved");
});

test("clearing the current page session preserves unrelated history state", () => {
  const history = memoryHistory({ unrelated: "preserved" });
  storePageSessionId(history, "workspace-a", "session-a");

  clearPageSessionId(history);

  assert.equal(readPageSessionId(history, "workspace-a"), "");
  assert.deepEqual(history.state, { unrelated: "preserved" });
});

test("a new page does not inherit an active session", () => {
  const currentPage = memoryHistory();
  storePageSessionId(currentPage, "workspace-a", "session-a");
  const newPage = memoryHistory();

  assert.equal(readPageSessionId(currentPage, "workspace-a"), "session-a");
  assert.equal(readPageSessionId(newPage, "workspace-a"), "");
});

test("the session route survives when browser history state is reconstructed", () => {
  const history = memoryHistory();
  const location = memoryLocation("?view=chat", "#latest");

  storePageSessionId(history, "workspace-a", "session-a", location);

  assert.match(history.url, /workspace=workspace-a/);
  assert.match(history.url, /session=session-a/);
  const reloadedLocation = memoryLocation(
    history.url.slice(history.url.indexOf("?"), history.url.indexOf("#")),
    "#latest",
  );
  assert.equal(
    readPageSessionId(memoryHistory(), "workspace-a", reloadedLocation),
    "session-a",
  );
  assert.equal(
    readPageSessionId(memoryHistory(), "workspace-b", reloadedLocation),
    "",
  );
});

test("clearing a session route preserves unrelated query parameters", () => {
  const history = memoryHistory();
  const location = memoryLocation(
    "?view=chat&workspace=workspace-a&session=session-a",
    "#latest",
  );

  clearPageSessionId(history, location);

  assert.equal(history.url, "/?view=chat#latest");
});
