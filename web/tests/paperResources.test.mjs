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

test("Paper profile reads the tree with cancellation and derives the publication badge", async () => {
  const original = globalThis.fetch;
  const requests = [];
  try {
    const controller = new AbortController();
    for (const publishState of ["published", "pending", "failed"]) {
      globalThis.fetch = async (path, init) => {
        requests.push({ path, ...init });
        return Response.json({ ...tree, publish_state: publishState });
      };
      const result = await paperProfile("project").fetch(controller.signal);
      assert.deepEqual(result.tree, { ...tree, publish_state: publishState });
      assert.equal(result.paper, publishState !== "published");
    }
    assert.ok(
      requests.every(
        ({ path, method, signal }) =>
          path === "/api/projects/project/paper/tree" && !method && signal === controller.signal,
      ),
    );
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
