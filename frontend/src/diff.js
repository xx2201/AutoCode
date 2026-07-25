export function parseDiff(raw) {
  const result = [];
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  for (const source of (raw || "").split("\n").slice(0, 5000)) {
    if (source.startsWith("@@")) {
      const match = source.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (match) {
        oldLine = Number(match[1]);
        newLine = Number(match[2]);
      }
      inHunk = true;
      result.push({ kind: "hunk", old: "", next: "", text: source });
    } else if (inHunk && source.startsWith("+") && !source.startsWith("+++")) {
      result.push({ kind: "added", old: "", next: newLine, text: source });
      newLine += 1;
    } else if (inHunk && source.startsWith("-") && !source.startsWith("---")) {
      result.push({ kind: "deleted", old: oldLine, next: "", text: source });
      oldLine += 1;
    } else if (inHunk && source.startsWith(" ")) {
      result.push({ kind: "context", old: oldLine, next: newLine, text: source });
      oldLine += 1;
      newLine += 1;
    } else {
      result.push({ kind: "meta", old: "", next: "", text: source });
    }
  }
  return result;
}
