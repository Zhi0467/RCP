import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "vite";

const root = fileURLToPath(new URL("..", import.meta.url));
const entry = resolve(root, "src/artifacts/artifactRenderer.tsx");
const syntax = resolve(root, "src/core/markdownSyntax.ts");
const require = createRequire(import.meta.url);
const stylesheet = require.resolve("katex/dist/katex.min.css");

export function artifactRenderer() {
  return {
    name: "rcp-artifact-renderer",
    apply: "build",
    async generateBundle() {
      for (const input of [entry, syntax, stylesheet]) this.addWatchFile(input);
      const rendered = await build({
        root,
        configFile: false,
        logLevel: "silent",
        define: { "process.env.NODE_ENV": JSON.stringify("production") },
        build: {
          write: false,
          minify: true,
          reportCompressedSize: false,
          lib: { entry, name: "RcpArtifactRenderer", formats: ["iife"] },
        },
      });
      const bundle = Array.isArray(rendered) ? rendered[0] : rendered;
      if (!("output" in bundle)) throw new Error("Artifact renderer build produced no bundle");
      const script = bundle.output.find((output) => output.type === "chunk");
      if (!script) throw new Error("Artifact renderer build produced no script");
      let css = (await readFile(stylesheet, "utf8")).replace(
        /,url\(fonts\/[^)]+\.(?:woff|ttf)\) format\("[^"]+"\)/g,
        "",
      );
      const sources = [
        ...new Set([...css.matchAll(/url\((fonts\/[^)]+)\)/g)].map((match) => match[1])),
      ];
      for (const source of sources) {
        const path = resolve(dirname(stylesheet), source);
        this.addWatchFile(path);
        const font = await readFile(path);
        css = css.replaceAll(
          `url(${source})`,
          `url(data:font/woff2;base64,${font.toString("base64")})`,
        );
      }
      this.emitFile({ type: "asset", fileName: "artifact-renderer.js", source: script.code });
      this.emitFile({ type: "asset", fileName: "artifact-renderer.css", source: css });
    },
  };
}
