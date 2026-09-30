import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const { RunArtifacts } = await server.ssrLoadModule("/src/components/RunArtifacts.tsx");
const artifact = {
  artifact_id: "pdf-one",
  name: "results.pdf",
  media_type: "application/pdf",
  view: "pdf",
  supplier: "turn",
  origin_operation_id: "turn-one",
  worker_label: "Worker 1",
  created_at: new Date().toISOString(),
};
function render() {
  return renderToStaticMarkup(
    React.createElement(RunArtifacts, { projectId: "project", artifacts: [artifact] }),
  );
}
test("browser run PDFs expose a download and no preview action", () => {
  const markup = render();
  assert.match(markup, /download="results.pdf"/);
  assert.match(markup, /href="\/api\/projects\/project\/artifacts\/pdf-one\/download"/);
  assert.doesNotMatch(markup, /<button|target="_blank"|\/viewer/);
});
test("desktop run PDFs retain a system-viewer action", () => {
  const previous = globalThis.window;
  globalThis.window = { __TAURI_INTERNALS__: {} };
  try {
    assert.match(render(), /<button[^>]+class="run-artifact-open"/);
  } finally {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  }
});
