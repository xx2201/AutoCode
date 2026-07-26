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
});
