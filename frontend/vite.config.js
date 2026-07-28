import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  base: "/assets/",
  build: {
    outDir: fileURLToPath(new URL("../autocode/web/static", import.meta.url)),
    assetsDir: ".",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks: {
          "markdown-vendor": ["react-markdown", "remark-gfm"],
        },
      },
    },
  },
});
