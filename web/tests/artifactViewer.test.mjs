import assert from "node:assert/strict";
import test from "node:test";
import {
  defaultViewerPlacement,
  parseViewerPlacement,
  viewerRect,
  floatViewer,
  moveViewer,
  resizeViewer,
  toggleViewerFullscreen,
  collapseViewer,
  acceptsArtifactEditMessage,
  artifactVersionChanged,
} from "../src/artifactViewerLayout.ts";

const viewport = { width: 1200, height: 800 };
test("viewer starts docked at the right and resizes against its fixed right edge", () => {
  const placement = parseViewerPlacement(null);
  assert.equal(placement.mode, "docked");
  const rect = viewerRect(placement, viewport);
  assert.equal(rect.x + rect.width, viewport.width);
  assert.equal(rect.height, viewport.height);
  assert.equal(rect.y, 0);
  const wider = viewerRect(resizeViewer(placement, -100, viewport), viewport);
  assert.equal(wider.width, rect.width + 100);
  assert.equal(wider.x + wider.width, viewport.width);
});
test("full screen restores floating placement and the dock tab restores docked placement", () => {
  const floating = moveViewer(
    floatViewer(defaultViewerPlacement, viewport),
    { x: -150, y: 30 },
    viewport,
  );
  assert.equal(floating.mode, "floating");
  assert.equal(floating.y, 30);
  assert.equal(floating.height, defaultViewerPlacement.height);
  const fullscreen = toggleViewerFullscreen(floating);
  assert.deepEqual(viewerRect(fullscreen, viewport), { x: 0, y: 0, ...viewport });
  assert.deepEqual(toggleViewerFullscreen(fullscreen), floating);
  for (const placement of [floating, fullscreen, defaultViewerPlacement]) {
    const restored = collapseViewer(collapseViewer(placement, true), false);
    assert.equal(restored.mode, "docked");
    assert.equal(restored.fullscreen, false);
    assert.equal(restored.collapsed, false);
    assert.equal(restored.width, placement.width);
  }
  assert.deepEqual(parseViewerPlacement(JSON.stringify(floating)), floating);
});
test("saved viewer geometry stays reachable on smaller screens and rejects corrupt preferences", () => {
  const small = { width: 260, height: 220 };
  const placement = { ...defaultViewerPlacement, mode: "floating", x: 9999, y: 9999 };
  assert.deepEqual(viewerRect(placement, small), { x: 0, y: 0, ...small });
  for (const raw of ["bad json", "null", "{}", JSON.stringify({ ...placement, width: -1 })]) {
    assert.deepEqual(parseViewerPlacement(raw), defaultViewerPlacement);
  }
});
test("only a versioned edit message from this iframe and RCP origin is accepted", () => {
  const frame = {};
  const origin = "https://rcp.example";
  const data = {
    type: "rcp-artifact-edit-started",
    version: 1,
    artifact_id: "a",
    operation_id: "op",
  };
  const event = { source: frame, origin, data };
  assert.equal(acceptsArtifactEditMessage(event, frame, origin, "a"), true);
  for (const invalid of [
    { ...event, source: {} },
    { ...event, origin: "null" },
    { ...event, origin: "https://other.example" },
    ...[
      null,
      "message",
      { ...data, version: 2 },
      { ...data, artifact_id: "b" },
      { ...data, operation_id: "" },
    ].map((data) => ({ ...event, data })),
  ])
    assert.equal(acceptsArtifactEditMessage(invalid, frame, origin, "a"), false);
  assert.equal(acceptsArtifactEditMessage({ ...event, source: null }, null, origin, "a"), false);
});
test("only a different current version requests a reload, including Undo", () => {
  assert.equal(artifactVersionChanged(null, "one"), false);
  assert.equal(artifactVersionChanged("one", "one"), false);
  assert.equal(artifactVersionChanged("one", "two"), true);
  assert.equal(artifactVersionChanged("two", "one"), true);
});
