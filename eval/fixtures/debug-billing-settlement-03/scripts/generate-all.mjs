import { promises as fs } from "node:fs";
        import path from "node:path";

        async function main() {
          const target = path.resolve("src/generated-snapshots/rebuild-marker.ts");
          await fs.mkdir(path.dirname(target), { recursive: true });
          await fs.writeFile(
            target,
            [
              "export const rebuildMarker = {",
              `  generatedAt: ${JSON.stringify(new Date().toISOString())},`,
              "  note: 'Benchmark fixture intentionally generated offline.'",
              "};",
              "",
            ].join("
"),
            "utf8",
          );
          console.log("generated snapshot marker");
        }

        main().catch((error) => {
          console.error(error);
          process.exitCode = 1;
        });
