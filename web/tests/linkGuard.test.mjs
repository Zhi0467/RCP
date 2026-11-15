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
const { leavesApp, linkClickAction } = await server.ssrLoadModule("/src/core/linkGuard.ts");

test("only hash routes on the app page stay in place", () => {
  const here = new URL("https://rcp.example:8421/#/projects/a?view=paper");
  assert.equal(leavesApp("#/projects/b", here), false);
  assert.equal(leavesApp("/#/projects/b", here), false);
  assert.equal(leavesApp("sections/methods.md", here), true);
  assert.equal(leavesApp("/api/projects/a/paper/files/f/raw", here), true);
  assert.equal(leavesApp("https://example.org/paper", here), true);
});

test("a same-space share link opens in place even from a link with a target", () => {
  const here = new URL("rcp-dev://localhost/#/projects/a");
  const access = "https://team.example.test";
  const click = (href, extra = {}) =>
    linkClickAction({ href, target: "", download: false, modified: false, ...extra }, here, access);
  const share = `${access}/open#l=tok`;
  assert.deepEqual(click(share), { kind: "share", token: "tok" });
  assert.deepEqual(click(share, { target: "_blank" }), { kind: "share", token: "tok" });
  assert.deepEqual(click(share, { modified: true, target: "_blank" }), { kind: "pass" });
  assert.deepEqual(click(share, { download: true }), { kind: "pass" });
  assert.deepEqual(click("https://example.org/x"), { kind: "new-tab" });
  assert.deepEqual(click("https://example.org/x", { target: "_blank" }), { kind: "pass" });
  assert.deepEqual(click("#/projects/b"), { kind: "pass" });
});
