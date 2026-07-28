import assert from "node:assert/strict";
import test from "node:test";

import { createSessionRequestCoordinator } from "../src/session-history.js";

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
