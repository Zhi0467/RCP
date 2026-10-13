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
after(() => server.close());
const { ResourceStore, resourceKeyId } = await server.ssrLoadModule("/src/core/resourceStore.ts");
const { graphResourceKey } = await server.ssrLoadModule("/src/graph/graphResources.ts");
const { cacheProjectTabState, projectReturnHash } = await server.ssrLoadModule(
  "/src/projects/useProjectTabs.ts",
);
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("opening a retained resource paints synchronously without waiting for a read", async () => {
  const store = new ResourceStore();
  const profile = { key: graphResourceKey("p"), fetch: async () => ({ revision: 3 }) };
  const id = store.register("p", profile);
  await settle();
  store.suspend("p", [id]);
  let resolve;
  store.register("p", {
    ...profile,
    fetch: () =>
      new Promise((done) => {
        resolve = done;
      }),
  });
  store.invalidate(id);
  assert.deepEqual(store.view(id).value, { revision: 3 });
  resolve({ revision: 4 });
  await settle();
  assert.deepEqual(store.view(id).value, { revision: 4 });
});

test("visited refs remain cached but only the current ref asks for watermarks", async () => {
  const store = new ResourceStore();
  const main = { key: graphResourceKey("p"), fetch: async () => "main" };
  const branch = {
    key: graphResourceKey("p", { kind: "branch", branch_id: "b" }),
    fetch: async () => "branch",
  };
  const mainId = store.register("p", main);
  await settle();
  store.suspend("p", [mainId]);
  store.register("p", branch);
  await settle();
  assert.deepEqual(store.requests("p"), [{ name: "graph", ref: "branch:b" }]);
  assert.equal(store.view(mainId).value, "main");
  store.release("p");
  assert.equal(store.view(mainId).value, undefined);
  assert.equal(store.view(resourceKeyId(branch.key)).value, undefined);
});

test("open tab UI has no retention count cap", () => {
  const cache = new Map();
  for (let i = 0; i < 20; i++) cacheProjectTabState(cache, String(i), { selected: i });
  assert.equal(cache.size, 20);
  assert.equal(
    projectReturnHash(new Map([["p", { kind: "branch", branch_id: "b" }]]), "p"),
    "/projects/p?branch_id=b",
  );
});
