import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import go from "highlight.js/lib/languages/go";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import php from "highlight.js/lib/languages/php";
import plaintext from "highlight.js/lib/languages/plaintext";
import powershell from "highlight.js/lib/languages/powershell";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

const languages = {
  bash,
  c,
  cpp,
  csharp,
  css,
  diff,
  go,
  java,
  javascript,
  json,
  markdown,
  php,
  plaintext,
  powershell,
  python,
  ruby,
  rust,
  sql,
  typescript,
  xml,
  yaml,
};

Object.entries(languages).forEach(([name, definition]) => {
  hljs.registerLanguage(name, definition);
});

const NAME_LANGUAGES = new Map([
  ["dockerfile", "bash"],
  ["makefile", "bash"],
  [".bashrc", "bash"],
  [".gitignore", "plaintext"],
]);

const EXTENSION_LANGUAGES = new Map([
  ["bash", "bash"],
  ["c", "c"],
  ["cc", "cpp"],
  ["cpp", "cpp"],
  ["cs", "csharp"],
  ["css", "css"],
  ["diff", "diff"],
  ["go", "go"],
  ["h", "c"],
  ["hpp", "cpp"],
  ["htm", "xml"],
  ["html", "xml"],
  ["java", "java"],
  ["js", "javascript"],
  ["jsx", "javascript"],
  ["json", "json"],
  ["jsonl", "json"],
  ["kt", "java"],
  ["kts", "java"],
  ["md", "markdown"],
  ["mdx", "markdown"],
  ["php", "php"],
  ["ps1", "powershell"],
  ["py", "python"],
  ["rb", "ruby"],
  ["rs", "rust"],
  ["sh", "bash"],
  ["sql", "sql"],
  ["svg", "xml"],
  ["toml", "plaintext"],
  ["ts", "typescript"],
  ["tsx", "typescript"],
  ["txt", "plaintext"],
  ["vue", "xml"],
  ["xml", "xml"],
  ["yaml", "yaml"],
  ["yml", "yaml"],
]);

export function languageForPath(path = "") {
  const name = path.replaceAll("\\", "/").split("/").at(-1)?.toLowerCase() || "";
  if (NAME_LANGUAGES.has(name)) return NAME_LANGUAGES.get(name);
  const extension = name.includes(".") ? name.split(".").at(-1) : "";
  return EXTENSION_LANGUAGES.get(extension) || "plaintext";
}

function splitHighlightedHtml(html) {
  const lines = [];
  const openTags = [];
  let current = "";
  const tokens = html.split(/(<span\b[^>]*>|<\/span>|\n)/);

  for (const token of tokens) {
    if (!token) continue;
    if (token === "\n") {
      current += "</span>".repeat(openTags.length);
      lines.push(current || "&nbsp;");
      current = openTags.join("");
    } else if (token.startsWith("<span")) {
      openTags.push(token);
      current += token;
    } else if (token === "</span>") {
      openTags.pop();
      current += token;
    } else {
      current += token;
    }
  }
  lines.push(current || "&nbsp;");
  return lines;
}

export function highlightCodeLines(code = "", path = "") {
  const language = languageForPath(path);
  const html = hljs.highlight(String(code), {
    language,
    ignoreIllegals: true,
  }).value;
  return splitHighlightedHtml(html);
}
