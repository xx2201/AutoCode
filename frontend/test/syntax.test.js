import test from "node:test";
import assert from "node:assert/strict";

import { highlightCodeLines, languageForPath } from "../src/syntax.js";

test("maps common project filenames to highlight.js languages", () => {
  assert.equal(languageForPath("src/example.py"), "python");
  assert.equal(languageForPath("frontend/App.jsx"), "javascript");
  assert.equal(languageForPath("scripts/deploy.ps1"), "powershell");
  assert.equal(languageForPath("README.md"), "markdown");
  assert.equal(languageForPath("unknown.custom"), "plaintext");
});

test("returns one safe highlighted fragment per source line", () => {
  const lines = highlightCodeLines(
    '"""First line\nSecond line"""\nif count > 2:\n    print("<safe>")',
    "example.py",
  );

  assert.equal(lines.length, 4);
  assert.match(lines[0], /hljs-string/);
  assert.match(lines[1], /hljs-string/);
  assert.match(lines[2], /hljs-keyword/);
  assert.match(lines[3], /&lt;safe&gt;/);
  assert.doesNotMatch(lines.join(""), /<safe>/);
});
