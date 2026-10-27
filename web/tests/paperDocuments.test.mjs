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
