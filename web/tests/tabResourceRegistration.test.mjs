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
const { ProjectTabResources } = await server.ssrLoadModule("/src/projects/projectTabResources.ts");
const { ResourceStore } = await server.ssrLoadModule("/src/core/resourceStore.ts");

test("replacement store registers active and hidden retained views afresh", () => {
  const original = new ProjectTabResources(new ResourceStore(), new Map());
  for (const project of ["active", "hidden"]) {
    const profile = {
      key: { backend: "", member: "", project, name: "graph", ref: "branch:b" },
      fetch: async () => ({ revision: 1 }),
    };
    original.profiles.set(project, [profile]);
    original.views.set(project, [original.store.register(project, profile)]);
    original.tabs.add(project);
  }
  const replacement = new ProjectTabResources(new ResourceStore(), original.profiles);
  replacement.restore();
  replacement.store.reset();
  replacement.restore();
  assert.deepEqual([...replacement.tabs], []);
  for (const project of ["active", "hidden"]) {
    assert.deepEqual(replacement.store.requests(project), [{ name: "graph", ref: "branch:b" }]);
    assert.deepEqual(replacement.views.get(project), original.views.get(project));
  }
});
