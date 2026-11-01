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
const { artifactStateProfile, artifactsProfile, runArtifactsProfile } = await server.ssrLoadModule(
  "/src/artifacts/artifactResources.ts",
);
const { researchViewsProfile } = await server.ssrLoadModule("/src/graph/researchViewResources.ts");
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("one artifact watermark refreshes inventory, run inventory, and shared viewer metadata", async () => {
  const store = new ResourceStore();
  store.setActiveProject("p");
  const profiles = [
    artifactsProfile("p"),
    runArtifactsProfile("p", "run"),
    artifactStateProfile("p", "a"),
    researchViewsProfile("p", null),
    researchViewsProfile("p", "branch"),
  ];
  const reads = profiles.map(() => 0);
  assert.notEqual(resourceKeyId(profiles[3].key), resourceKeyId(profiles[4].key));
  const ids = profiles.map((profile, i) =>
    store.register("p", { ...profile, fetch: async () => ++reads[i] }),
  );
  store.register("viewer", { ...profiles[2], fetch: async () => ++reads[2] });
  await settle();
  assert.deepEqual(reads, [1, 1, 1, 1, 1]);
  assert.deepEqual(store.requests("p"), [{ name: "artifacts" }, { name: "chats" }]);
  store.observe("p", [
    { name: "artifacts", version: "moved" },
    { name: "chats", version: "1" },
  ]);
  await settle();
  assert.deepEqual(reads, [2, 2, 2, 2, 2]);
  store.observe("p", [
    { name: "artifacts", version: "moved" },
    { name: "chats", version: "2" },
  ]);
  await settle();
  assert.deepEqual(reads, [2, 3, 2, 2, 2]);
  store.write(ids[2], "undo");
  assert.equal(store.view(resourceKeyId(artifactStateProfile("p", "a").key)).value, "undo");
  store.release("viewer");
  assert.equal(store.view(ids[2]).value, "undo");
  store.release("p");
  assert.deepEqual(store.requests("p"), []);
});

test("report opening shares pending and cached store reads and requires its bound runtime", async () => {
  const { bindArtifactResourceStore, openEpisodeReport, closeArtifactViewer } =
    await server.ssrLoadModule("/src/artifacts/artifactViewerModel.ts");
  const store = new ResourceStore();
  const unbind = bindArtifactResourceStore(store);
  const profile = runArtifactsProfile("p", "run");
  let finish;
  store.register("p", {
    ...profile,
    fetch: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  });
  const opening = openEpisodeReport({ projectId: "p", episodeId: "run" });
  closeArtifactViewer();
  finish([{ supplier: "episode_ending", artifact_id: "report" }]);
  await opening;
  const id = resourceKeyId(profile.key);
  assert.equal(store.view(id).value[0].artifact_id, "report");
  await openEpisodeReport({ projectId: "p", episodeId: "run" });
  unbind();
  await assert.rejects(openEpisodeReport({ projectId: "p", episodeId: "run" }), /unavailable/);
  store.reset();
});
