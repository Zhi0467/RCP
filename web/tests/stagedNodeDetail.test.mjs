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
const { StagedNodeEditor } = await server.ssrLoadModule("/src/graph/NewCustomNode.tsx");
test("staged field editor requires known origin and preserves valid zero and false fields", () => {
  const props = {
    node: {
      id: "ev/new",
      type: "evidence",
      title: "Observation",
      observation: "Measured",
      origin: "unknown",
      extension_type: null,
      extension_fields: { score: 0, verified: false },
    },
    ontology: {
      types: [],
      relations: [],
      fields: [
        { owner_type: "evidence", name: "score", kind: "number", required: true },
        { owner_type: "evidence", name: "verified", kind: "boolean", required: true },
      ],
    },
    disabled: false,
    onSave() {},
    onCancel() {},
  };
  const render = (origin) =>
    renderToStaticMarkup(
      React.createElement(StagedNodeEditor, { ...props, node: { ...props.node, origin } }),
    );
  assert.match(render("unknown"), /type="submit" disabled=""/);
  const valid = render("analytic");
  assert.doesNotMatch(valid, /type="submit" disabled/);
  assert.match(valid, /type="number" value="0"/);
  assert.match(valid, /value="false" selected=""/);
});
