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

test("Paper profile reads only the tree through its API base with cancellation", async () => {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, init) => {
    requests.push({ path, ...init });
    return Response.json(tree);
  };
  try {
    const controller = new AbortController();
    const profile = paperProfile("project", "/api/projects/project");
    const result = await profile.fetch(controller.signal);
    assert.deepEqual(
      requests.map(({ path }) => path),
      ["/api/projects/project/paper/tree"],
    );
    assert.ok(requests.every(({ method, signal }) => !method && signal === controller.signal));
    assert.deepEqual(result.tree, tree);
  } finally {
    globalThis.fetch = original;
  }
});

test("Paper shell badge reflects publication independently of the saved tree revision", async () => {
  const original = globalThis.fetch;
  try {
    for (const publishState of ["published", "pending", "failed"]) {
      globalThis.fetch = async () => Response.json({ ...tree, publish_state: publishState });
      const result = await paperProfile("p").fetch(new AbortController().signal);
      assert.equal(result.tree.saved_revision, 4);
      assert.equal(result.paper, publishState !== "published");
    }
  } finally {
    globalThis.fetch = original;
  }
});

test("opening the same paper link creates another navigation request", async () => {
  const { paperFileRequest } = await server.ssrLoadModule("/src/paper/paperResources.ts");
  const first = paperFileRequest("#/projects/p/paper?paper_file=A");
  const again = paperFileRequest("#/projects/p/paper?paper_file=A");
  assert.equal(again.fileId, "A");
  assert.notEqual(again, first);
});
