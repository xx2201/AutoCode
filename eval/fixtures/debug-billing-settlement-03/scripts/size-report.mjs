import { promises as fs } from "node:fs";
import path from "node:path";

async function collect(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...await collect(fullPath));
    } else if (entry.isFile() && fullPath.endsWith(".ts")) {
      files.push(fullPath);
    }
  }
  return files;
}

const files = await collect(path.resolve("src"));
let bytes = 0;
let lines = 0;
for (const file of files) {
  const content = await fs.readFile(file, "utf8");
  bytes += Buffer.byteLength(content, "utf8");
  lines += content.split(/\r?\n/).length;
}

console.log(`${files.length} files | ${lines} lines | ${(bytes / 1024).toFixed(1)} KB`);
