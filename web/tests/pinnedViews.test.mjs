import assert from "node:assert/strict";
import test from "node:test";
import { acceptsArtifactOpenNodeMessage } from "../src/artifacts/artifactViewerLayout.ts";

test("open-node accepts only the active same-origin shell and an existing presented node", () => {
  const frame = {};
  const data = { type: "rcp-open-node", version: 1, artifact_id: "a", node_id: "n" };
  const event = { source: frame, origin: "https://rcp.test", data };
  const accepts = (e = event, ready = true) =>
    acceptsArtifactOpenNodeMessage(e, frame, event.origin, "a", ready, { n: {} });
  assert.equal(accepts(), true);
  for (const e of [
    { ...event, source: {} },
    { ...event, origin: "null" },
    ...[
      null,
      {},
      { ...data, version: 2 },
      { ...data, artifact_id: "b" },
      { ...data, node_id: "missing" },
      { ...data, node_id: "toString" },
      { ...data, type: "other" },
    ].map((data) => ({ ...event, data })),
  ])
    assert.equal(accepts(e), false);
  assert.equal(accepts(event, false), false);
});
