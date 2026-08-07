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
const { canonicalRevisionNeedsReload, canonicalRevisionPollDelay, loadCanonicalRevision } =
  await server.ssrLoadModule("/src/App.tsx");

after(() => server.close());

test("canonical revision polling uses only the lightweight project endpoint", async () => {
  const requested = [];
  const revision = await loadCanonicalRevision(async (path) => {
    requested.push(path);
    return { revision: 12 };
  }, "/api/projects/project-1");

  assert.equal(revision, 12);
  assert.deepEqual(requested, ["/api/projects/project-1/revision"]);
});

test("canonical state reloads only after the accepted revision advances", () => {
  assert.equal(canonicalRevisionNeedsReload(8, 7), true);
  assert.equal(canonicalRevisionNeedsReload(7, 7), false);
  assert.equal(canonicalRevisionNeedsReload(6, 7), false);
});

test("canonical revision polling backs off after failures and stays bounded", () => {
  assert.equal(canonicalRevisionPollDelay(0), 2_000);
  assert.equal(canonicalRevisionPollDelay(1), 4_000);
  assert.equal(canonicalRevisionPollDelay(4), 30_000);
  assert.equal(canonicalRevisionPollDelay(20), 30_000);
});
