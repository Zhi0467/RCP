import assert from "node:assert/strict";
import { test } from "node:test";
import { readReferenceFiles } from "../src/paper/referenceFiles.ts";

test("all bib files load by hash, changed hashes reload, deleted files leave the cache", async (t) => {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url) => {
    calls.push(url);
    return new Response("@article{one, title={First}}");
  });
  const cache = new Map();
  const signal = new AbortController().signal;
  const file = { file_id: "f1", path: "a.bib", sha256: "one" };
  const other = { file_id: "f2", path: "b.bib", sha256: "two" };
  const entries = await readReferenceFiles(
    "p",
    [file, other, { path: "main.tex" }],
    1,
    signal,
    cache,
  );
  assert.deepEqual(
    entries.map((entry) => [entry.key, entry.file.path]),
    [
      ["one", "a.bib"],
      ["one", "b.bib"],
    ],
  );
  await readReferenceFiles("p", [file, other], 2, signal, cache);
  assert.equal(calls.length, 2);
  await readReferenceFiles("p", [{ ...file, sha256: "new" }], 3, signal, cache);
  assert.equal(calls.length, 3);
  assert.equal(calls[2], "/api/projects/p/paper/files/f1/raw?revision=3");
  assert.deepEqual([...cache.keys()], ["p:new"]);
});

test("failed reads reject and aborted reads never fill the cache", async (t) => {
  const controller = new AbortController();
  const cache = new Map();
  const files = [{ file_id: "f", path: "a.bib", sha256: "hash" }];
  t.mock.method(globalThis, "fetch", async () => new Response("", { status: 503 }));
  await assert.rejects(readReferenceFiles("p", files, 1, controller.signal, cache));
  globalThis.fetch = async () => {
    controller.abort();
    return new Response("@misc{k}");
  };
  await readReferenceFiles("p", files, 1, controller.signal, cache);
  assert.equal(cache.size, 0);
});
