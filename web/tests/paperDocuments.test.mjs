import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as Y from "yjs";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { paperDocumentKey, vectorCovers } = await server.ssrLoadModule(
  "/src/paper/paperDocuments.ts",
);
after(() => server.close());

test("document identity separates backend, member, project and stable file id", () => {
  const key = { backend: "a", member: "b", project: "c", file: "d" };
  const keys = new Set([paperDocumentKey(key)]);
  for (const field of Object.keys(key)) keys.add(paperDocumentKey({ ...key, [field]: "other" }));
  assert.equal(keys.size, 5);
  assert.notEqual(
    paperDocumentKey({ ...key, backend: "a:b", member: "c" }),
    paperDocumentKey({ ...key, backend: "a", member: "b:c" }),
  );
});

test("durable coverage requires every local client clock, including offline merged edits", () => {
  const first = new Y.Doc();
  const second = new Y.Doc();
  first.getText("text").insert(0, "first");
  Y.applyUpdate(second, Y.encodeStateAsUpdate(first));
  const ack = Y.encodeStateVector(first);
  assert.equal(vectorCovers(ack, Y.encodeStateVector(second)), true);
  second.getText("text").insert(0, "second");
  assert.equal(vectorCovers(ack, Y.encodeStateVector(second)), false);
  Y.applyUpdate(first, Y.encodeStateAsUpdate(second));
  assert.equal(vectorCovers(Y.encodeStateVector(first), Y.encodeStateVector(second)), true);
  first.destroy();
  second.destroy();
});

test("a delete-only edit advances durability beyond an older in-flight acknowledgement", async () => {
  const { trackDeletionClock } = await server.ssrLoadModule("/src/paper/paperDocuments.ts");
  const doc = new Y.Doc();
  const text = doc.getText("text");
  trackDeletionClock(doc, text);
  text.insert(0, "draft");
  const ack = Y.encodeStateVector(doc);
  text.delete(0, 2);
  assert.equal(vectorCovers(ack, Y.encodeStateVector(doc)), false);
  assert.equal(text.toString(), "aft");
  const replica = new Y.Doc();
  Y.applyUpdate(replica, Y.encodeStateAsUpdate(doc));
  assert.equal(replica.getText("text").toString(), "aft");
  assert.equal(vectorCovers(Y.encodeStateVector(replica), Y.encodeStateVector(doc)), true);
  doc.destroy();
  replica.destroy();
});

test("retaining a released document restores full awareness", async () => {
  const { PaperDocument } = await server.ssrLoadModule("/src/paper/paperDocuments.ts");
  globalThis.window = { location: { href: "http://localhost/" } };
  const owner = new PaperDocument(
    { backend: "b", member: "m", project: "p", file: "f" },
    "/api/projects/p",
    "Writer",
  );
  try {
    const release = owner.retain();
    owner.provider.awareness.setLocalStateField("cursor", { anchor: 2 });
    const before = owner.provider.awareness.getLocalState();
    release();
    assert.equal(owner.provider.awareness.getLocalState(), null);
    const releaseAgain = owner.retain();
    assert.deepEqual(owner.provider.awareness.getLocalState(), before);
    releaseAgain();
  } finally {
    owner.destroy();
    delete globalThis.window;
  }
});

test("backend reverification preserves owners until an observed identity changes", async () => {
  const { paperBackendIdentityTracker } = await server.ssrLoadModule(
    "/src/paper/paperDocuments.ts",
  );
  const changed = paperBackendIdentityTracker();
  const health = { instance_id: "i", data_dir_id: "d", version: "v" };
  assert.equal(changed({ ok: true, health }), false);
  assert.equal(changed({ ok: true, health: { ...health } }), false);
  assert.equal(changed({ ok: false, health: null }), false);
  assert.equal(changed({ ok: false, health: { ...health } }), false);
  assert.equal(changed({ ok: false, health: { ...health, instance_id: "new" } }), true);
});

test("current UTF-16 anchors follow unrelated local edits and reject stale ranges", async () => {
  const { anchorPaperChange, resolvePaperAnchor } = await server.ssrLoadModule(
    "/src/paper/paperAnchors.ts",
  );
  const doc = new Y.Doc();
  const text = doc.getText("text");
  text.insert(0, "😀one\ntwo");
  const insertion = {
    current: { start: 6, end: 6, revision: 2, text: "" },
    preview: { removed: [] },
  };
  const anchor = anchorPaperChange(insertion, text);
  assert.ok(anchor);
  text.insert(2, "changed ");
  assert.deepEqual(resolvePaperAnchor(anchor, insertion, text), { from: 14, to: 14 });
  const edit = {
    current: { start: 14, end: 17, revision: 3, text: "two" },
    preview: { removed: ["two"] },
  };
  const editAnchor = anchorPaperChange(edit, text);
  assert.ok(editAnchor);
  text.insert(15, "x");
  assert.equal(resolvePaperAnchor(editAnchor, edit, text), null);
  for (const current of [
    null,
    { start: -1, end: -1 },
    { start: 999, end: 999 },
    { start: 5, end: 2 },
  ])
    assert.equal(anchorPaperChange({ ...insertion, current }, text), null);
  doc.destroy();
});

test("repeated lines use the verified current range and hide after that range changes", async () => {
  const { anchorPaperChange, resolvePaperAnchor } = await server.ssrLoadModule(
    "/src/paper/paperAnchors.ts",
  );
  const doc = new Y.Doc();
  const text = doc.getText("text");
  text.insert(0, "same\nsame");
  const change = {
    lines: { start: 1, end: 1 },
    current: { start: 5, end: 9, revision: 1, text: "same" },
    preview: { removed: ["same"] },
  };
  const anchor = anchorPaperChange(change, text);
  assert.deepEqual(resolvePaperAnchor(anchor, change, text), { from: 5, to: 9 });
  text.insert(6, "x");
  assert.equal(resolvePaperAnchor(anchor, change, text), null);
  assert.equal(anchorPaperChange({ ...change, current: null }, text), null);
  doc.destroy();
});

test("permanent rejection exposes failure and disables automatic reconnect", async () => {
  const { PaperDocument } = await server.ssrLoadModule("/src/paper/paperDocuments.ts");
  globalThis.window = { location: { href: "http://localhost/" } };
  const owner = new PaperDocument(
    { backend: "b", member: "m", project: "p", file: "f" },
    "/api/projects/p",
    "Writer",
  );
  try {
    for (const code of [4400, 4413]) assert.equal(owner.provider.shouldReconnect({ code }), false);
    assert.equal(owner.provider.shouldReconnect({ code: 4529 }), true);
    owner.provider.emit("closed", [{ code: 4400 }]);
    assert.ok(owner.getSnapshot().error);
  } finally {
    owner.destroy();
    delete globalThis.window;
  }
});

test("rejected reconnect replaces retained oversized Yjs history and deletes only that file cache", async () => {
  const { PaperDocument } = await server.ssrLoadModule("/src/paper/paperDocuments.ts");
  globalThis.window = { location: { href: "http://localhost/" } };
  const key = { backend: "b", member: "m", project: "p", file: "rejected" };
  const owner = new PaperDocument(key, "/api/projects/p", "Writer");
  const other = new PaperDocument({ ...key, file: "other" }, "/api/projects/p", "Writer");
  const oldDoc = owner.doc;
  const undo = new Y.UndoManager(owner.text);
  const deleted = [];
  try {
    owner.text.insert(0, "x".repeat(5 * 1024 * 1024));
    owner.text.delete(0, owner.text.length);
    other.text.insert(0, "unsaved");
    assert.ok(Y.encodeStateAsUpdate(oldDoc).byteLength > 4 * 1024 * 1024);
    owner.provider.emit("closed", [{ code: 4413 }]);
    globalThis.indexedDB = {
      deleteDatabase(name) {
        deleted.push(name);
        const request = {};
        queueMicrotask(() => request.onsuccess());
        return request;
      },
    };
    await owner.reconnect();
    assert.notEqual(owner.doc, oldDoc);
    assert.ok(Y.encodeStateAsUpdate(owner.doc).byteLength < 100);
    assert.deepEqual(deleted, [`rcp:paper:${paperDocumentKey(key)}`]);
    assert.equal(other.text.toString(), "unsaved");
  } finally {
    undo.destroy();
    owner.destroy();
    other.destroy();
    delete globalThis.indexedDB;
    delete globalThis.window;
  }
});
