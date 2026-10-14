import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

const [shell, content] = JSON.parse(
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `
import json
from rcp.artifacts import AgentArtifactDescriptor
from rcp.artifact_views import artifact_viewer_document, artifact_content
d = AgentArtifactDescriptor(artifact_id="a"*24,name="board.html",media_type="text/html",size_bytes=1)
page = b'''<!doctype html><button id="node">Node</button><script>
addEventListener('message', e => { if(e.source === parent && e.data?.kind === 'rcp-live-data') document.body.dataset.delivered='yes'; });
document.getElementById('node').onclick=()=>parent.postMessage({type:'rcp-open-node',version:1,id:'n'},'*');
</script>'''
print(json.dumps([artifact_viewer_document(d,content_url="/content",state="task",live_url="/live"), artifact_content("board.html","text/html",page)]))
`,
    ],
    { encoding: "utf8" },
  ),
);

test("opaque page traverses only its wrapper and viewer; stale mark survives notices at desktop and phone widths", async () => {
  const browser = await chromium.launch();
  try {
    for (const width of [1280, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 800 } });
      const errors = [],
        heads = [];
      page.on("pageerror", (error) => errors.push(error.message));
      let failed = false;
      await page.route("http://rcp.test/**", (route) => {
        const url = new URL(route.request().url());
        if (url.pathname === "/app")
          return route.fulfill({
            contentType: "text/html",
            body: '<meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0}iframe{width:100%;height:95dvh;border:0}</style><iframe src="/viewer"></iframe><script>window.received=[];addEventListener("message",e=>{if(e.data?.type==="rcp-open-node")received.push(e.data)});</script>',
          });
        if (url.pathname === "/viewer")
          return route.fulfill({
            contentType: "text/html",
            body: shell[0],
            headers: { "Content-Security-Policy": shell[1] },
          });
        if (url.pathname === "/content")
          return route.fulfill({
            contentType: content[1],
            body: content[0],
            headers: { "Content-Security-Policy": content[2] },
          });
        if (url.pathname === "/live") {
          heads.push(url.searchParams.get("head"));
          const graph = failed
            ? { kind: "graph", error: "graph_too_large" }
            : url.searchParams.has("head")
              ? { kind: "graph", head: "h", unchanged: true }
              : { kind: "graph", head: "h", nodes: [{ id: "n" }], edges: [] };
          return route.fulfill({
            json: {
              kind: "rcp-live-data",
              version: 1,
              refresh_seconds: 0.05,
              complete: !failed,
              snapshots: [graph],
            },
          });
        }
        return route.fulfill({ status: 404, body: "" });
      });
      await page.goto("http://rcp.test/app");
      const viewer = page.frameLocator("iframe");
      const wrapper = viewer.frameLocator("#preview");
      const agent = wrapper.frameLocator("#artifact");
      await agent.locator("body[data-delivered=yes]").waitFor();
      await agent.locator("#node").click();
      await page.waitForFunction(() => received.length === 1);
      assert.deepEqual(await page.evaluate(() => received), [
        { type: "rcp-open-node", version: 1, artifact_id: "a".repeat(24), node_id: "n" },
      ]);
      // Direct page-to-shell and sibling-to-wrapper spoofing cannot bypass a hop.
      await agent
        .locator("body")
        .evaluate(() =>
          parent.parent.postMessage({ type: "rcp-open-node", version: 1, id: "n" }, "*"),
        );
      await viewer
        .locator("#preview")
        .evaluate((frame) =>
          frame.contentWindow.postMessage({ type: "rcp-open-node", version: 1, id: "n" }, "*"),
        );
      failed = true;
      await viewer.locator("#preview[data-graph-stale=true]").waitFor();
      assert.notEqual(
        await viewer.locator("#preview").evaluate((el) => getComputedStyle(el).outlineStyle),
        "none",
      );
      failed = false;
      await viewer.locator("#preview[data-graph-stale=false]").waitFor();
      assert.equal(await page.evaluate(() => received.length), 1);
      assert.equal(heads[0], null);
      assert.ok(heads.slice(1).every((head) => head === "h"));
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await browser.close();
  }
});
