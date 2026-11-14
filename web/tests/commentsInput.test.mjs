import assert from "node:assert/strict";
import test from "node:test";
import { selectionInput } from "../src/comments/selectionInput.ts";

test("frame input accepts anchors and strips authority and comment fields", () => {
  assert.deepEqual(
    selectionInput({
      kind: "element",
      path: "#chart",
      label: "Chart",
      text: "42",
      comment: "send",
      thread_ids: ["x"],
    }),
    { kind: "element", path: "#chart", label: "Chart", text: "42", comment: "" },
  );
  assert.deepEqual(
    selectionInput({
      kind: "pdf",
      page: 2,
      rect: { x: 0, y: 0, width: 1, height: 1 },
      text: "Quote",
    }),
    { kind: "pdf", page: 2, rect: { x: 0, y: 0, width: 1, height: 1 }, text: "Quote", comment: "" },
  );
});

test("frame input refuses invalid geometry and oversized selections", () => {
  for (const raw of [
    null,
    {},
    { kind: "text", text: "x".repeat(4097) },
    { kind: "pdf", page: 0, rect: { x: 0, y: 0, width: 1, height: 1 } },
    {
      kind: "box",
      rect: { x: 0.9, y: 0, width: 0.2, height: 1 },
      viewport: { width: 100, height: 100 },
      elements: [],
    },
  ])
    assert.equal(selectionInput(raw), null);
});

test("displayed version comes from the pinned content URL", async () => {
  const { displayedVersion } = await import("../src/comments/viewerModel.ts");
  assert.equal(
    displayedVersion(
      "/api/artifacts/a/content?version_id=old&error_channel=x",
      "https://rcp.example",
    ),
    "old",
  );
  assert.equal(displayedVersion("/preview", "https://rcp.example"), null);
});
