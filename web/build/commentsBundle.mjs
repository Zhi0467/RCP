import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { build } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const entry = resolve(root, "src/comments/viewer.ts");

export function commentsBundle() {
  return {
    name: "rcp-comments",
    apply: "build",
    async generateBundle() {
      this.addWatchFile(entry);
      const rendered = await build({
        root,
        configFile: false,
        logLevel: "silent",
        define: { "process.env.NODE_ENV": JSON.stringify("production") },
        build: {
          write: false,
          minify: true,
          reportCompressedSize: false,
          lib: { entry, name: "RcpComments", formats: ["iife"] },
        },
      });
      const bundle = Array.isArray(rendered) ? rendered[0] : rendered;
      if (!("output" in bundle)) throw new Error("Comments build produced no bundle");
      const script = bundle.output.find((output) => output.type === "chunk");
      const style = bundle.output.find(
        (output) => output.type === "asset" && output.fileName.endsWith(".css"),
      );
      if (!script || !style) throw new Error("Comments build produced no script or stylesheet");
      for (const id of Object.keys(script.modules)) this.addWatchFile(id);
      this.emitFile({ type: "asset", fileName: "comments.js", source: script.code });
      this.emitFile({ type: "asset", fileName: "comments.css", source: style.source });
    },
  };
}
