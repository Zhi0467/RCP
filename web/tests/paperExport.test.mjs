import assert from "node:assert/strict";
import { test } from "node:test";
import { fetchArxivExport, paperArxivUrl } from "../src/paper/arxivExport.ts";

test("export URL encodes the selected project and file", () => {
  assert.equal(paperArxivUrl("a/b", "c d"), "/api/projects/a%2Fb/paper/export/arxiv?file_id=c%20d");
});
test("export returns the ZIP or structured refusal without losing the log", async (t) => {
  t.mock.method(
    globalThis,
    "fetch",
    async () => new Response("zip", { headers: { "Content-Type": "application/zip" } }),
  );
  const result = await fetchArxivExport("p", "f");
  assert.equal(await result.blob.text(), "zip");
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ reason: "not_root", log_tail: "diagnostic" }), { status: 409 });
  assert.deepEqual(await fetchArxivExport("p", "f"), {
    reason: "not_root",
    log_tail: "diagnostic",
  });
});
