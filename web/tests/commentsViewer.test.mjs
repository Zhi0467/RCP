import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

const artifactId = "a".repeat(24);
const origin = "http://rcp.test";
const documents = JSON.parse(
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `import json
from rcp.artifacts import AgentArtifactDescriptor, html_preview_document
from rcp.artifact_comments import comment_panel, selection_frame_addon
from rcp.artifact_views import artifact_viewer_document
source = '''<style>body{margin:0;min-height:2200px}#first,#second{position:absolute;left:20px;width:100px;height:40px;background:tan}#first{top:100px}#second{top:1000px}</style><div id="first">First anchor</div><div id="second">Second anchor</div><script>
window.attack=async()=>{
  const forged={type:'rcp-artifact-position',version:1,id:'first',rect:{x:1,y:1,width:1,height:1}};
  parent.postMessage(forged,'*');
  top.postMessage(forged,'*');
  parent.postMessage({kind:'rcp-artifact-position',id:'first',rect:forged.rect},'*');
  top.postMessage({type:'rcp-artifact-selection',version:1,selection:{kind:'whole',comment:''}},'*');
  try { await fetch('http://rcp.test/api/projects/p/comment-threads',{method:'POST',body:'{}'}); return 'sent'; }
  catch { return 'blocked'; }
};
</script>'''
d = AgentArtifactDescriptor(artifact_id='a'*24,name='page.html',media_type='text/html',size_bytes=len(source))
shell = artifact_viewer_document(d,content_url='/content',state='temporary',panel=comment_panel({'projectId':'p','artifactId':d.artifact_id,'mediaType':'text/html'}))
content = html_preview_document(source.encode(),frame_addon=selection_frame_addon())
print(json.dumps({'shell':shell,'content':content}))`,
    ],
    { cwd: new URL("../..", import.meta.url), encoding: "utf8", maxBuffer: 20 * 1024 * 1024 },
  ),
);

const threads = ["first", "second"].map((id) => ({
  thread_id: id,
  project_id: "p",
  target_kind: "artifact",
  target_id: artifactId,
  graph_target: "main",
  drawn_version: null,
  anchor: { kind: "element", path: `#${id}`, label: id, text: "", comment: "" },
  state: "open",
  created_by: "member",
  created_at: "2026-11-01T12:00:00Z",
  closed_by: null,
  closed_at: null,
  sent_operation_id: null,
  can_send: true,
  posts: [
    {
      post_id: `post-${id}`,
      thread_id: id,
      author: { user_id: "member", display_name: "Reader" },
      text: `Review ${id}`,
      created_at: "2026-11-01T12:00:00Z",
    },
  ],
}));

for (const width of [1040, 390]) {
  test(`generated HTML viewer locates through its private bridge at ${width}px`, async () => {
    const browser = await chromium.launch();
    try {
      const page = await browser.newPage({ viewport: { width, height: 844 } });
      const errors = [];
      const writes = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route(`${origin}/**`, async (route) => {
        const request = route.request();
        const path = new URL(request.url()).pathname;
        if (request.method() !== "GET") writes.push(path);
        const rendered =
          path === "/viewer" ? documents.shell : path === "/content" ? documents.content : null;
        if (rendered) {
          await route.fulfill({
            contentType: "text/html",
            headers: { "Content-Security-Policy": rendered[1] },
            body: rendered[0],
          });
        } else if (path.endsWith("/state")) {
          await route.fulfill({ json: { can_comment: true, fresh_session_required: false } });
        } else if (path.endsWith("/comment-threads")) {
          await route.fulfill({ json: { threads } });
        } else await route.fulfill({ status: 404 });
      });
      await page.goto(`${origin}/viewer`);
      const artifact = page.frameLocator("#preview").frameLocator("#artifact");
      const firstPin = page.getByRole("button", { name: "Comment 1", exact: true });
      await firstPin.waitFor();
      const initialPosition = await firstPin.boundingBox();
      assert.equal(await artifact.locator("body").evaluate(() => window.attack()), "blocked");
      await page.evaluate(
        () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
      );
      assert.deepEqual(await firstPin.boundingBox(), initialPosition);
      assert.equal(
        await page.getByRole("dialog", { name: "Comment", exact: true }).isVisible(),
        false,
      );
      assert.deepEqual(writes, []);
      await firstPin.click();
      assert.ok(
        await page
          .locator('[data-thread-id="first"]')
          .evaluate((card) => card.classList.contains("selected")),
      );
      await page.locator('[data-thread-id="second"] .rcp-comment-quote').click();
      const secondPin = page.getByRole("button", { name: "Comment 2", exact: true });
      await secondPin.waitFor();
      await artifact.locator('[data-rcp-selection="locate"]').waitFor();
      await artifact.locator("#second").evaluate(() => window.scrollBy(0, 180));
      const target = await artifact.locator("#second").boundingBox();
      await page.waitForFunction(
        ({ x, y }) => {
          const pin = document.querySelector('[aria-label="Comment 2"]');
          return (
            pin &&
            Math.abs(parseFloat(pin.style.left) - x) < 1 &&
            Math.abs(parseFloat(pin.style.top) - y) < 1
          );
        },
        { x: target.x + target.width, y: target.y + target.height },
      );
      assert.ok(
        await page
          .locator('[data-thread-id="second"]')
          .evaluate((card) => card.classList.contains("selected")),
      );
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      if (width === 390) {
        const rail = await page.locator("#comments-root > .rcp-comments").boundingBox();
        assert.equal(rail.x, 0);
        assert.equal(rail.width, 390);
        assert.ok(Math.abs(rail.y + rail.height - 844) < 1);
      }
      assert.deepEqual(errors, []);
    } finally {
      await browser.close();
    }
  });
}
