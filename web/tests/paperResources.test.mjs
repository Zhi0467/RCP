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
  files: [{ file_id: "f1", path: "intro.md", kind: "text", format: "markdown", size: 5 }],
};

test("Paper profile reads the tree with cancellation", async () => {
  const original = globalThis.fetch;
  const requests = [];
  try {
    const controller = new AbortController();
    globalThis.fetch = async (path, init) => {
      requests.push({ path, ...init });
      return Response.json(tree);
    };
    const result = await paperProfile("project").fetch(controller.signal);
    assert.deepEqual(result.tree, tree);
    assert.deepEqual(
      requests.map(({ path, method, signal }) => [path, method, signal === controller.signal]),
      [["/api/projects/project/paper/tree", undefined, true]],
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
