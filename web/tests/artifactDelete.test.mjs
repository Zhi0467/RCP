import assert from "node:assert/strict";
import test from "node:test";
import { artifactAlreadyGone, artifactDeleteUrl } from "../src/artifacts/artifactDelete.ts";

test("delete targets the encoded artifact and treats an already-gone artifact as deleted", () => {
  const url = new URL(artifactDeleteUrl("project one", "a/b"), "https://rcp.example");
  assert.deepEqual(url.pathname.split("/").map(decodeURIComponent), [
    "",
    "api",
    "projects",
    "project one",
    "artifacts",
    "a/b",
  ]);
  for (const status of [404, 410]) assert.equal(artifactAlreadyGone({ status }), true);
  for (const failure of [{ status: 409 }, new Error("x"), null]) {
    assert.equal(artifactAlreadyGone(failure), false);
  }
});
