import assert from "node:assert/strict";
import { test } from "node:test";
import { readReferenceFiles, findReference } from "../src/paper/referenceFiles.ts";

test("reference identity survives offset changes and selects the first duplicate in its file", () => {
  const entries = [
    { key: "one", from: 0, file: { file_id: "other" } },
    { key: "one", from: 30, file: { file_id: "bib" } },
    { key: "one", from: 80, file: { file_id: "bib" } },
  ];
  assert.equal(findReference(entries, { key: "one", fileId: "bib" }), entries[1]);
  entries[1].from += 50;
  assert.equal(findReference(entries, { key: "one", fileId: "bib" }), entries[1]);
  assert.equal(findReference(entries, { key: "one" }), entries[0]);
  assert.equal(findReference(entries, { key: "absent", fileId: "bib" }), undefined);
  assert.equal(findReference(entries, null), undefined);
});

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
  const entries = await readReferenceFiles("p", [file, other, { path: "main.tex" }], signal, cache);
  assert.deepEqual(
    entries.map((entry) => [entry.key, entry.file.path]),
    [
      ["one", "a.bib"],
      ["one", "b.bib"],
    ],
  );
  await readReferenceFiles("p", [file, other], signal, cache);
  assert.equal(calls.length, 2);
  await readReferenceFiles("p", [{ ...file, sha256: "new" }], signal, cache);
  assert.equal(calls.length, 3);
  assert.equal(calls[2], "/api/projects/p/paper/files/f1/raw?sha256=new");
  assert.deepEqual([...cache.keys()], ["p:new"]);
});

test("failed reads remain visible and aborted reads never fill the cache", async (t) => {
  const controller = new AbortController();
  const cache = new Map();
  const files = [{ file_id: "f", path: "a.bib", sha256: "hash" }];
  t.mock.method(globalThis, "fetch", async () => new Response("", { status: 503 }));
  const entries = await readReferenceFiles("p", files, controller.signal, cache);
  assert.deepEqual(
    entries.map(({ key, unreadable }) => [key, unreadable]),
    [["", true]],
  );
  globalThis.fetch = async () => {
    controller.abort();
    return new Response("@misc{k}");
  };
  await readReferenceFiles("p", files, controller.signal, cache);
  assert.equal(cache.size, 0);
});

for (const failure of ["http", "network", "body"]) {
  test(`one ${failure} failure preserves readable files and can retry`, async (t) => {
    const files = [
      { file_id: "good", path: "good.bib", sha256: "good" },
      { file_id: "bad", path: "bad.bib", sha256: "bad" },
    ];
    t.mock.method(globalThis, "fetch", async (url) => {
      if (url.includes("/good/")) return new Response("@misc{kept}");
      if (failure === "network") throw new TypeError("offline");
      if (failure === "body")
        return {
          ok: true,
          text: async () => {
            throw new Error("read failed");
          },
        };
      return new Response("", { status: 503 });
    });
    const cache = new Map();
    const signal = new AbortController().signal;
    const entries = await readReferenceFiles("p", files, signal, cache);
    assert.deepEqual(
      entries.map(({ key, unreadable, file }) => [file.file_id, key, unreadable]),
      [
        ["good", "kept", false],
        ["bad", "", true],
      ],
    );
    assert.deepEqual([...cache.keys()], ["p:good"]);
    globalThis.fetch = async () => new Response("@misc{recovered}");
    assert.deepEqual(
      (await readReferenceFiles("p", files, signal, cache)).map(({ key }) => key),
      ["kept", "recovered"],
    );
  });
}
