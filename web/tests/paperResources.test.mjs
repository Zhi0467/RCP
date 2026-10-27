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
const { paperProfile } = await server.ssrLoadModule("/src/paper/paperResources.ts");
after(() => server.close());

const tree = {
  revision: 4,
  saved_revision: 4,
  published_revision: 3,
  publish_state: "pending",
  hold: null,
  files: [{ file_id: "f1", path: "intro.md", kind: "text", format: "markdown", size: 5 }],
};

test("Paper profile reads the tree and sessions through its API base with shared cancellation", async () => {
  const original = globalThis.fetch;
  const requests = [];
  const sessions = [{ native_session_id: "s1" }];
  globalThis.fetch = async (path, init) => {
    requests.push({ path, ...init });
    return Response.json(path.endsWith("/sessions") ? sessions : tree);
  };
  try {
    const controller = new AbortController();
    const profile = paperProfile("project", "/api/projects/project");
    const result = await profile.fetch(controller.signal);
    assert.deepEqual(
      requests.map(({ path }) => path),
      ["/api/projects/project/paper/tree", "/api/projects/project/paper/sessions"],
    );
    assert.ok(requests.every(({ method, signal }) => !method && signal === controller.signal));
    assert.deepEqual(result.tree, tree);
    assert.deepEqual(result.sessions, sessions);
    assert.equal(result.paper.content, "");
    assert.equal(result.paper.draft_version, null);
  } finally {
    globalThis.fetch = original;
  }
});

test("Paper shell badge reflects publication independently of the saved tree revision", async () => {
  const original = globalThis.fetch;
  try {
    for (const publishState of ["published", "pending", "failed"]) {
      globalThis.fetch = async (path) =>
        Response.json(path.endsWith("/sessions") ? [] : { ...tree, publish_state: publishState });
      const result = await paperProfile("p").fetch(new AbortController().signal);
      assert.equal(result.tree.saved_revision, 4);
      assert.equal(result.paper.sync_state === "synced", publishState === "published");
      assert.equal(result.paper.canonical_available, true);
    }
  } finally {
    globalThis.fetch = original;
  }
});
