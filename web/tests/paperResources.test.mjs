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

test("Paper refuses a stale draft version and reloads through its ordinary profile", async () => {
  const previousFetch = globalThis.fetch;
  const paper = { content: "newer saved draft", draft_version: "v2", sync_state: "synced" };
  const requests = [];
  globalThis.fetch = async (path, init) => {
    requests.push({ path, ...init });
    if (init.method === "PUT")
      return new Response(
        JSON.stringify({ detail: "The Paper draft changed since it was loaded." }),
        {
          status: 409,
        },
      );
    return Response.json(path.endsWith("sessions") ? [] : paper);
  };
  try {
    const store = new ResourceStore();
    const profile = paperProfile("p");
    const id = store.register("tab", profile);
    await new Promise(setImmediate);
    const buffer = { content: "typed buffer", draftVersion: "v1" };
    await assert.rejects(
      savePaperDraft(store, profile, buffer.content, "base", buffer.draftVersion),
      { status: 409, message: "The Paper draft changed since it was loaded." },
    );
    await new Promise(setImmediate);
    assert.deepEqual(JSON.parse(requests.find((request) => request.method === "PUT").body), {
      content: "typed buffer",
      base_hash: "base",
      draft_version: "v1",
    });
    assert.equal(
      requests.filter((request) => request.path.endsWith("/paper") && !request.method).length,
      2,
    );
    assert.equal(store.view(id).value.paper.draft_version, "v2");
    assert.deepEqual(buffer, { content: "typed buffer", draftVersion: "v1" });
    assert.deepEqual(store.view(id).pending, []);
    assert.equal(resourceKeyId(profile.key), id);
    store.release("tab");
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("a known absent Paper stays accepted while a changed token is refetched", async () => {
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
  finish({ paper: { sync_state: "not_created", draft_version: null }, sessions: [] });
  await new Promise(setImmediate);
  assert.equal(store.view(id).stale, false);
  store.observe("p", [{ name: "paper", version: "changed" }]);
  assert.equal(store.view(id).value.paper.sync_state, "not_created");
  assert.equal(store.view(id).stale, false);
  store.release("p");
});

test("a draft loaded without a stored version sends an explicit null guard", async () => {
  const previousFetch = globalThis.fetch;
  let savedBody;
  const paper = { content: "draft", draft_version: "created", sync_state: "synced" };
  globalThis.fetch = async (path, init) => {
    if (init.method === "PUT") savedBody = JSON.parse(init.body);
    return Response.json(path.endsWith("sessions") ? [] : paper);
  };
  const store = new ResourceStore();
  try {
    const profile = paperProfile("p");
    const id = store.register("p", profile);
    await new Promise(setImmediate);
    await savePaperDraft(store, profile, "draft", null, null);
    assert.deepEqual(savedBody, { content: "draft", base_hash: null, draft_version: null });
    assert.equal(store.view(id).value.paper.draft_version, "created");
  } finally {
    store.release("p");
    globalThis.fetch = previousFetch;
  }
});
