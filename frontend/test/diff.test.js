import assert from "node:assert/strict";
import test from "node:test";
import { parseDiff, parseDiffHunks } from "../src/diff.js";

const NEW_FILE_DIFF = `diff --git a/哈哈哈.txt b/哈哈哈.txt
new file mode 100644
--- /dev/null
+++ b/哈哈哈.txt
@@ -0,0 +1,1 @@
+哈哈哈`;

test("diff parser omits raw Git metadata and keeps only code changes", () => {
  const lines = parseDiff(NEW_FILE_DIFF);

  assert.deepEqual(lines, [
    { kind: "hunk", old: "", next: "", text: "第 1-1 行" },
    {
      kind: "added",
      old: "",
      next: 1,
      marker: "+",
      text: "+哈哈哈",
    },
  ]);
  assert.equal(JSON.stringify(lines).includes("diff --git"), false);
  assert.equal(JSON.stringify(lines).includes("/dev/null"), false);
});

test("diff parser groups hunks with responsive-view statistics", () => {
  const hunks = parseDiffHunks(`@@ -10,2 +10,3 @@
 keep
-old
+new
+added`);

  assert.equal(hunks.length, 1);
  assert.equal(hunks[0].additions, 2);
  assert.equal(hunks[0].deletions, 1);
  assert.equal(hunks[0].lines[0].text, "keep");
});

test("diff parser preserves hunks longer than 5000 source lines", () => {
  const source = [
    "@@ -1,6000 +1,6000 @@",
    ...Array.from({ length: 6000 }, (_, index) => ` line-${index + 1}`),
  ].join("\n");

  const hunks = parseDiffHunks(source);

  assert.equal(hunks.length, 1);
  assert.equal(hunks[0].lines.length, 6000);
  assert.equal(hunks[0].lines.at(-1).text, "line-6000");
});
