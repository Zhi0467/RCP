import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import react from "@vitejs/plugin-react";
import { build } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));

async function iife(entry, name) {
  const rendered = await build({
    root,
    configFile: false,
    logLevel: "silent",
    plugins: [react()],
    define: { "process.env.NODE_ENV": JSON.stringify("production") },
    build: {
      write: false,
      minify: true,
      reportCompressedSize: false,
      lib: { entry, name, formats: ["iife"] },
    },
  });
  const bundle = Array.isArray(rendered) ? rendered[0] : rendered;
  if (!("output" in bundle)) throw new Error(`${name} build produced no bundle`);
  const script = bundle.output.find((output) => output.type === "chunk");
  if (!script) throw new Error(`${name} build produced no script`);
  const style = bundle.output.find(
    (output) => output.type === "asset" && output.fileName.endsWith(".css"),
  );
  return { script, style };
}

export function commentsBundle() {
  return {
    name: "rcp-comments",
    apply: "build",
    async generateBundle() {
      const viewer = await iife(resolve(root, "src/comments/viewer.tsx"), "RcpComments");
      const text = await iife(resolve(root, "src/comments/anchors.ts"), "RcpCommentText");
      if (!viewer.style) throw new Error("Comments build produced no stylesheet");
      for (const id of [...Object.keys(viewer.script.modules), ...Object.keys(text.script.modules)])
        this.addWatchFile(id);
      this.emitFile({ type: "asset", fileName: "comments.js", source: viewer.script.code });
      this.emitFile({ type: "asset", fileName: "comments.css", source: viewer.style.source });
      this.emitFile({ type: "asset", fileName: "comment-text.js", source: text.script.code });
    },
  };
}
