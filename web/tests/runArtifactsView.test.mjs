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
const { RunArtifacts } = await server.ssrLoadModule("/src/experiments/RunArtifacts.tsx");
const artifact = {
  artifact_id: "pdf-one",
  name: "results.pdf",
  download_name: "results.pdf",
  media_type: "application/pdf",
  view: "pdf",
  supplier: "turn",
  origin_operation_id: "turn-one",
  worker_label: "Worker 1",
  created_at: new Date().toISOString(),
};
function render(entry = artifact) {
  return renderToStaticMarkup(
    React.createElement(RunArtifacts, { projectId: "project", artifacts: [entry] }),
  );
}
test("run PDFs open in the viewer in a browser too", () => {
  assert.match(render(), /<button[^>]+class="run-artifact-open"/);
});

test("desktop stored files with no producing task expose a native download button", () => {
  const previous = globalThis.window;
  globalThis.window = { __TAURI_INTERNALS__: {} };
  try {
    const markup = render({ ...artifact, view: "file", origin_operation_id: null });
    assert.match(markup, /<button/);
    assert.doesNotMatch(markup, /<a[ >]/);
  } finally {
    if (previous === undefined) delete globalThis.window;
    else globalThis.window = previous;
  }
});
