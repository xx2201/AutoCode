import assert from "node:assert/strict";
import test from "node:test";

import { mergeResultMessages } from "../src/features/conversation/model.js";

test("synchronized result attaches changed files and downloadable outputs once", () => {
  const messages = [
    { role: "user", content: "生成文件" },
    { role: "assistant", content: "完成" },
  ];
  const result = {
    text: "完成",
    files: [{ file_id: "f-1", name: "result.txt" }],
  };
  const changedFiles = [{ path: "result.txt", additions: 1, deletions: 0 }];

  const merged = mergeResultMessages([], messages, result, changedFiles);

  assert.deepEqual(merged[0].changed_files, changedFiles);
  assert.deepEqual(merged[1].files, result.files);
  assert.equal(merged.length, 2);
});

test("fallback result preserves current history and appends one assistant answer", () => {
  const current = [{ role: "user", content: "继续" }];
  const merged = mergeResultMessages(current, [], { text: "已继续" });

  assert.deepEqual(merged, [
    ...current,
    { role: "assistant", content: "已继续", files: [] },
  ]);
});
