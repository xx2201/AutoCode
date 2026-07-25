export function parseDiffHunks(raw) {
  const hunks = [];
  let current = null;
  let oldLine = 0;
  let newLine = 0;
  for (const source of (raw || "").split("\n").slice(0, 5000)) {
    if (source.startsWith("@@")) {
      const match = source.match(
        /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/,
      );
      if (!match) continue;
      oldLine = Number(match[1]);
      newLine = Number(match[3]);
      current = {
        oldStart: oldLine,
        oldCount: Number(match[2] ?? 1),
        newStart: newLine,
        newCount: Number(match[4] ?? 1),
        additions: 0,
        deletions: 0,
        lines: [],
      };
      hunks.push(current);
    } else if (current && source.startsWith("+") && !source.startsWith("+++")) {
      current.lines.push({
        kind: "added",
        old: "",
        next: newLine,
        marker: "+",
        text: source.slice(1),
      });
      current.additions += 1;
      newLine += 1;
    } else if (current && source.startsWith("-") && !source.startsWith("---")) {
      current.lines.push({
        kind: "deleted",
        old: oldLine,
        next: "",
        marker: "−",
        text: source.slice(1),
      });
      current.deletions += 1;
      oldLine += 1;
    } else if (current && source.startsWith(" ")) {
      current.lines.push({
        kind: "context",
        old: oldLine,
        next: newLine,
        marker: "",
        text: source.slice(1),
      });
      oldLine += 1;
      newLine += 1;
    }
  }
  return hunks;
}

export function parseDiff(raw) {
  return parseDiffHunks(raw).flatMap((hunk) => [
    {
      kind: "hunk",
      old: "",
      next: "",
      text: `第 ${hunk.newStart}-${Math.max(hunk.newStart, hunk.newStart + hunk.newCount - 1)} 行`,
    },
    ...hunk.lines.map((line) => ({
      ...line,
      text: `${line.marker}${line.text}`,
    })),
  ]);
}
