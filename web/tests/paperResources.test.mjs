import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { ResourceStore, resourceKeyId } = await server.ssrLoadModule("/src/core/resourceStore.ts");
const { paperProfile, savePaperDraft } = await server.ssrLoadModule("/src/paper/paperResources.ts");
after(() => server.close());

test("Paper refuses a stale draft revision and reloads through its ordinary profile", async () => {
  const previousFetch = globalThis.fetch;
  const paper = { content: "newer saved draft", draft_revision: 2, sync_state: "synced" };
  const requests = [];
  globalThis.fetch = async (path, init) => {
    requests.push({ path, ...init });
    if (init.method === "PUT")
      return new Response(JSON.stringify({ detail: { code: "paper_draft_moved" } }), {
        status: 409,
      });
    return Response.json(path.endsWith("sessions") ? [] : paper);
  };
  try {
    const store = new ResourceStore();
    const profile = paperProfile("p");
    const id = store.register("tab", profile);
    await new Promise(setImmediate);
    const buffer = { content: "typed buffer", draftRevision: 1 };
    await assert.rejects(
      savePaperDraft(store, profile, buffer.content, "base", buffer.draftRevision),
      { status: 409, code: "paper_draft_moved" },
    );
    await new Promise(setImmediate);
    assert.deepEqual(JSON.parse(requests.find((request) => request.method === "PUT").body), {
      content: "typed buffer",
      base_hash: "base",
      draft_revision: 1,
    });
    assert.equal(
      requests.filter((request) => request.path.endsWith("/paper") && !request.method).length,
      2,
    );
    assert.equal(store.view(id).value.paper.draft_revision, 2);
    assert.deepEqual(buffer, { content: "typed buffer", draftRevision: 1 });
    assert.deepEqual(store.view(id).pending, []);
    assert.equal(resourceKeyId(profile.key), id);
    store.release("tab");
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("a known absent Paper becomes stale as soon as its token moves", async () => {
  const store = new ResourceStore();
  let finish;
  const profile = {
    key: paperProfile("p").key,
    fetch: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  };
  const id = store.register("p", profile);
  finish({ paper: { sync_state: "not_created", draft_revision: 0 }, sessions: [] });
  await new Promise(setImmediate);
  assert.equal(store.view(id).stale, false);
  store.observe("p", [{ name: "paper", version: "changed" }]);
  assert.equal(store.view(id).value.paper.sync_state, "not_created");
  assert.equal(store.view(id).stale, true);
  store.release("p");
});
