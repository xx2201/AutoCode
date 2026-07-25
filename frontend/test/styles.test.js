import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("assistant prose inherits the Codex-style answer typography", () => {
  const rule = styles.match(/\.turn-answer \.rich-text p\s*\{([^}]*)\}/)?.[1];

  assert.ok(rule, "expected a dedicated assistant prose rule");
  assert.match(rule, /font-size:\s*inherit/);
  assert.match(rule, /line-height:\s*inherit/);
});

test("markdown code blocks render as light inline work instead of a dark panel", () => {
  const rule = styles.match(/\.rich-text pre\s*\{([^}]*)\}/)?.[1];

  assert.ok(rule, "expected a markdown code block rule");
  assert.match(rule, /background:\s*transparent/);
  assert.match(rule, /border-left:\s*2px solid/);
  assert.doesNotMatch(rule, /#292c3b|#292b35/);
});
