import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
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
const { RefreshRuntimeContext, useResource } = await server.ssrLoadModule(
  "/src/core/useResource.ts",
);
const { useProjectResource } = await server.ssrLoadModule("/src/projects/projectResources.ts");
const { experimentIndexProfile } = await server.ssrLoadModule(
  "/src/experiments/episodeResources.ts",
);
const { cacheProjectTabState, projectReturnHash, useProjectTabs } = await server.ssrLoadModule(
  "/src/projects/useProjectTabs.ts",
);
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("shared resource hooks withhold retained data during render before any layout effect", async () => {
  const store = new ResourceStore();
  const profile = { key: graphResourceKey("p"), fetch: async () => "retained payload" };
  store.register("cache", profile);
  await settle();
  function Resource() {
    return useResource("panel", profile).value ?? "loading";
  }
  function ProjectResource() {
    return useProjectResource(profile).value ?? "loading";
  }
  const rendered = renderToStaticMarkup(
    React.createElement(
      RefreshRuntimeContext.Provider,
      { value: { store } },
      React.createElement(Resource),
      React.createElement(ProjectResource),
    ),
  );
  assert.doesNotMatch(rendered, /retained payload/);
  assert.equal(rendered, "loadingloading");
  assert.equal(store.view(resourceKeyId(profile.key)).value, "retained payload");
});

test("retained cache stays available while a new presentation waits for its current read", async () => {
  const store = new ResourceStore();
  const profile = { key: graphResourceKey("p"), fetch: async () => ({ revision: 3 }) };
  const id = store.register("p", profile);
  await settle();
  store.suspend("p", [id]);
  store.beginPresentation("panel", "return", [id]);
  let resolve;
  store.register("p", {
    ...profile,
    fetch: () =>
      new Promise((done) => {
        resolve = done;
      }),
  });
  assert.deepEqual(store.view(id).value, { revision: 3 });
  assert.deepEqual(store.presentationView("panel", "return").views, []);
  assert.equal(store.view(id).stale, false, "pending validation is not a failed read");
  resolve({ revision: 4 });
  await settle();
  assert.deepEqual(store.view(id).value, { revision: 4 });
  assert.deepEqual(store.presentationView("panel", "return").views[0].value, { revision: 4 });
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
    projectReturnHash(new Map([["p", "/projects/p/agents?branch_id=b"]]), "p"),
    "/projects/p/agents?branch_id=b",
  );
});

test("refreshing a project's Runs waits for the read it asked for", async () => {
  const store = new ResourceStore();
  let resolveRead;
  store.register("p", {
    ...experimentIndexProfile("p"),
    fetch: () => new Promise((resolve) => (resolveRead = resolve)),
  });
  let tabs;
  function Harness() {
    tabs = useProjectTabs({
      initialProjectId: null,
      initialSetupOpen: false,
      projectIndexReady: false,
      project: null,
      reportError() {},
    });
    return null;
  }
  renderToStaticMarkup(
    React.createElement(
      RefreshRuntimeContext.Provider,
      { value: { store } },
      React.createElement(Harness),
    ),
  );
  resolveRead({ entries: ["old"], unavailable: [] });
  await settle();
  let refreshed;
  void tabs.refreshProjectExperimentLoops("p").then((entries) => (refreshed = entries));
  await settle();
  assert.equal(refreshed, undefined, "the refresh settles with its read");
  resolveRead({ entries: ["new"], unavailable: [] });
  await settle();
  assert.deepEqual(refreshed, ["new"]);
});
