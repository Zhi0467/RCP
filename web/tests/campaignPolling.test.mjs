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
const { watcherResourceKey, episodeResourceKey } = await server.ssrLoadModule(
  "/src/experiments/episodeResources.ts",
);
after(() => server.close());

test("a late watcher read cannot publish into the newly selected branch", async () => {
  const store = new ResourceStore();
  store.setActiveProject("p");
  const main = watcherResourceKey("p", { kind: "main" });
  const branch = watcherResourceKey("p", { kind: "branch", branch_id: "b" });
  let finishMain;
  const mainId = store.register("tab", {
    key: main,
    fetch: () =>
      new Promise((resolve) => {
        finishMain = resolve;
      }),
  });
  store.release("tab", [mainId]);
  const branchId = store.register("tab", { key: branch, fetch: async () => ["branch watcher"] });
  await new Promise(setImmediate);
  finishMain(["main watcher"]);
  await new Promise(setImmediate);
  assert.deepEqual(store.view(branchId).value, ["branch watcher"]);
  assert.equal(store.view(mainId).value, undefined);
  assert.notEqual(resourceKeyId(main), resourceKeyId(branch));
});

test("an episode mutation fences an older timeline read", async () => {
  const store = new ResourceStore();
  let finish;
  const id = store.register("tab", {
    key: episodeResourceKey("p", "e"),
    fetch: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  });
  const value = {
    episode: { episode_id: "e", status: "stopped" },
    messages: ["saved"],
    timeline: {},
  };
  store.write(id, value);
  finish({ episode: { status: "running" }, messages: [], timeline: {} });
  await new Promise(setImmediate);
  assert.strictEqual(store.view(id).value, value);
});
