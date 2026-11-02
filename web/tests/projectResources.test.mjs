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
const { digestProfile, questionsProfile, settingsProfile } = await server.ssrLoadModule(
  "/src/projects/projectResources.ts",
);
const { ResourceStore, resourceKeyId } = await server.ssrLoadModule("/src/core/resourceStore.ts");
after(() => server.close());
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("project watermarks refresh target-specific digest and question payloads", async () => {
  const store = new ResourceStore();
  store.setActiveProject("p");
  const profiles = [
    digestProfile("p", { kind: "main" }),
    digestProfile("p", { kind: "branch", branch_id: "b" }),
    questionsProfile("p", "chat", "c"),
  ];
  const reads = profiles.map(() => 0);
  profiles.forEach((profile, index) =>
    store.register("p", { ...profile, fetch: async () => ++reads[index] }),
  );
  await settle();
  store.observe("p", [
    { name: "digest", version: "1" },
    { name: "chats", version: "1" },
    { name: "questions", version: "1" },
  ]);
  await settle();
  assert.deepEqual(reads, [2, 2, 2]);
  store.observe("p", [
    { name: "digest", version: "1" },
    { name: "chats", version: "1" },
    { name: "questions", version: "1" },
  ]);
  await settle();
  assert.deepEqual(reads, [2, 2, 2]);
  store.observe("p", [
    { name: "digest", version: "1" },
    { name: "chats", version: "2" },
    { name: "questions", version: "1" },
  ]);
  await settle();
  assert.deepEqual(reads, [3, 3, 2]);
  assert.notEqual(resourceKeyId(profiles[0].key), resourceKeyId(profiles[1].key));
});

test("a settings command fences readiness read before publishing the probe response", async () => {
  const store = new ResourceStore();
  let finish;
  const profile = settingsProfile("p");
  const id = store.register("p", {
    ...profile,
    fetch: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  });
  const mutation = store.beginMutation("probe", new Map([[id, (value) => value]]));
  mutation.settle(new Map([[id, { provider_readiness: { fresh: true } }]]));
  finish({ provider_readiness: { stale: true } });
  await settle();
  assert.deepEqual(store.view(id).value, { provider_readiness: { fresh: true } });
});

test("a previous digest target stays cached but dormant until reopened", async () => {
  const store = new ResourceStore();
  store.setActiveProject("p");
  const profile = digestProfile("p", { kind: "branch", branch_id: "old" });
  let reads = 0;
  const read = { ...profile, fetch: async () => ++reads };
  const id = store.register("p", read);
  await settle();
  store.suspend("p", [id]);
  assert.equal(store.view(id).value, 1);
  assert.deepEqual(store.requests("p"), []);
  store.observe("p", [
    { name: "digest", version: "2" },
    { name: "chats", version: "1" },
  ]);
  await settle();
  assert.equal(reads, 1);
  store.register("p", read);
  store.observe("p", [
    { name: "digest", version: "2" },
    { name: "chats", version: "1" },
  ]);
  await settle();
  assert.ok(reads > 1);
  assert.equal(store.view(id).value, reads);
  store.release("p");
  assert.equal(store.view(id).value, undefined);
});

test("settings refresh carries manifest changes without replacing graph or paper", async (context) => {
  const snapshot = {
    name: "renamed",
    machines: [{ alias: "new-host" }],
    skill_defaults: { project_chat: ["new-skill"] },
    graph: { revision: 99 },
    paper: { text: "unrelated" },
    project_truth_scope: ["protected"],
  };
  const readiness = {
    provider_readiness: { fresh: true },
    providers: {},
    provider_skill_inventories: {},
    agent_profiles: {},
  };
  const controller = new AbortController();
  context.mock.method(globalThis, "fetch", async (path, init) => {
    assert.equal(init.signal, controller.signal);
    return Response.json(path.endsWith("/readiness") ? readiness : snapshot);
  });
  const value = await settingsProfile("p").fetch(controller.signal);
  assert.equal(value.name, snapshot.name);
  assert.deepEqual(value.machines, snapshot.machines);
  assert.deepEqual(value.skill_defaults, snapshot.skill_defaults);
  assert.deepEqual(value.provider_readiness, readiness.provider_readiness);
  for (const field of ["graph", "paper", "project_truth_scope"])
    assert.equal(field in value, false);
});

test("view lifetime keeps shared tab interest until the last active subscriber leaves", async () => {
  const { retainProjectView } = await server.ssrLoadModule("/src/projects/projectResources.ts");
  const store = new ResourceStore();
  store.setActiveProject("p");
  const profile = {
    key: { backend: "", member: "", project: "p", name: "terminals" },
    fetch: async () => [],
  };
  const first = retainProjectView(store, profile);
  const second = retainProjectView(store, profile);
  first();
  assert.equal(store.requests("p").length, 1);
  second();
  assert.equal(store.requests("p").length, 0);
  const hidden = retainProjectView(store, profile);
  store.setActiveProject("other");
  hidden();
  assert.equal(store.requests("p").length, 1);
  store.release("p");
  assert.equal(store.requests("p").length, 0);
});
