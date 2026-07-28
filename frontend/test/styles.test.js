import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import test from "node:test";

const styleDirectory = new URL("../src/styles/", import.meta.url);
const styleSources = Object.fromEntries(
  readdirSync(styleDirectory)
    .filter((name) => name.endsWith(".css"))
    .map((name) => [
      name,
      readFileSync(new URL(name, styleDirectory), "utf8"),
    ]),
);
const styles = Object.values(styleSources).join("\n");

test("assistant prose inherits the Codex-style answer typography", () => {
  const rule = styles.match(/\.turn-answer \.rich-text\s*\{([^}]*)\}/)?.[1];

  assert.ok(rule, "expected a dedicated assistant prose rule");
  assert.match(rule, /font-size:\s*inherit/);
  assert.match(rule, /line-height:\s*inherit/);
});

test("Markdown headings, lists, tables, quotes, and inline code have semantic styles", () => {
  assert.match(styles, /\.rich-text h1,/);
  assert.match(styles, /\.rich-text ul,/);
  assert.match(styles, /\.rich-text blockquote\s*\{/);
  assert.match(styles, /\.rich-text :not\(pre\) > code\s*\{/);
  assert.match(styles, /\.rich-text table\s*\{/);
});

test("markdown code blocks render as light inline work instead of a dark panel", () => {
  const rule = styles.match(/\.rich-text pre\s*\{([^}]*)\}/)?.[1];

  assert.ok(rule, "expected a markdown code block rule");
  assert.match(rule, /background:\s*transparent/);
  assert.match(rule, /border-left:\s*2px solid/);
  assert.doesNotMatch(rule, /#292c3b|#292b35/);
});

test("approval request uses a neutral inline card instead of the legacy warning banner", () => {
  const rule = styles.match(/\.approval-request\s*\{([^}]*)\}/)?.[1];

  assert.ok(rule, "expected an inline approval request rule");
  assert.match(rule, /border:\s*1px solid #dfe1e7/);
  assert.match(rule, /background:\s*#fff/);
  assert.doesNotMatch(styles, /\.approval-banner|#fffaf0/);
});

test("mobile approval actions fit the viewport without horizontal scrolling", () => {
  const mobile = styleSources["responsive.css"]
    .match(/@media \(max-width: 720px\)\s*\{([\s\S]*)\}\s*$/)?.[1];

  assert.ok(mobile, "expected the mobile media query");
  assert.match(mobile, /\.approval-request-actions\s*\{[^}]*grid-template-columns:\s*1fr 1fr/);
  assert.match(mobile, /\.approval-request-target\s*\{[^}]*grid-template-columns:\s*1fr/);
});
