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
const { leavesApp } = await server.ssrLoadModule("/src/core/linkGuard.ts");

test("only hash routes on the app page stay in place", () => {
  const here = new URL("https://rcp.example:8421/#/projects/a?view=paper");
  assert.equal(leavesApp("#/projects/b", here), false);
  assert.equal(leavesApp("/#/projects/b", here), false);
  assert.equal(leavesApp("sections/methods.md", here), true);
  assert.equal(leavesApp("/api/projects/a/paper/files/f/raw", here), true);
  assert.equal(leavesApp("https://example.org/paper", here), true);
});
