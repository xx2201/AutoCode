import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import test from "node:test";

import RichText from "../src/markdown.js";

const markdownPath = new URL("../src/markdown.js", import.meta.url);

test("memoizes unchanged Markdown answers", async () => {
  const source = await readFile(markdownPath, "utf8");

  assert.match(source, /import \{ createElement, memo \} from "react"/);
  assert.match(source, /export default memo\(RichText\)/);
});

test("renders assistant Markdown as semantic HTML", () => {
  const html = renderToStaticMarkup(createElement(RichText, {
    content: [
      "# 项目架构",
      "",
      "这是 **重点** 和 `inline()`。",
      "",
      "1. 浏览器调用网关",
      "2. 网关调用服务",
      "",
      "> 重点结论",
      "",
      "| 组件 | 协议 |",
      "| --- | --- |",
      "| 网关 | HTTP |",
      "",
      "```text",
      "browser -> gateway",
      "```",
    ].join("\n"),
  }));

  assert.match(html, /<h1>项目架构<\/h1>/);
  assert.match(html, /<strong>重点<\/strong>/);
  assert.match(html, /<code>inline\(\)<\/code>/);
  assert.match(html, /<ol>/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /<table>/);
  assert.match(html, /<pre><code class="language-text">browser -&gt; gateway/);
  assert.doesNotMatch(html, /># 项目架构</);
});

test("keeps external links safe and opens them outside the workspace", () => {
  const safe = renderToStaticMarkup(createElement(RichText, {
    content: "[官网](https://example.com)",
  }));
  const unsafe = renderToStaticMarkup(createElement(RichText, {
    content: "[危险链接](javascript:alert(1))",
  }));

  assert.match(safe, /href="https:\/\/example\.com"/);
  assert.match(safe, /target="_blank"/);
  assert.match(safe, /rel="noreferrer noopener"/);
  assert.doesNotMatch(unsafe, /href=/);
});
