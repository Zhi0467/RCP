import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

const artifactId = "a".repeat(24);
const origin = "http://rcp.test";
const secureOrigin = "https://rcp.test";
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
window.clicks=0;document.getElementById('second').addEventListener('click',()=>window.clicks++);
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
      const dialog = page.getByRole("dialog", { name: "Comment", exact: true });
      await artifact.locator("#second").click();
      assert.equal(await artifact.locator("body").evaluate(() => window.clicks), 1);
      assert.equal(await dialog.isVisible(), false);
      await page.getByRole("button", { name: "Comment on a part" }).click();
      await artifact.locator("#second").click();
      await dialog.waitFor();
      assert.equal(await artifact.locator("body").evaluate(() => window.clicks), 1);
      await page.keyboard.press("Escape");
      await page.getByRole("button", { name: "Done commenting" }).click();
      await artifact.locator("#second").click();
      assert.equal(await artifact.locator("body").evaluate(() => window.clicks), 2);
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

function sendableThread(index, extra = {}) {
  const id = `t${String(index).padStart(3, "0")}`;
  return {
    ...threads[0],
    thread_id: id,
    anchor: { kind: "whole", comment: "" },
    created_at: new Date(Date.UTC(2026, 10, 1, 12, 0, index)).toISOString(),
    posts: [{ ...threads[0].posts[0], post_id: `post-${id}`, thread_id: id }],
    ...extra,
  };
}

async function openViewer(browser, list, answer) {
  const page = await browser.newPage({ viewport: { width: 1040, height: 844 } });
  const errors = [];
  const sends = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let current = list;
  await page.route(`${secureOrigin}/**`, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const rendered =
      path === "/viewer" ? documents.shell : path === "/content" ? documents.content : null;
    if (rendered) {
      await route.fulfill({
        contentType: "text/html",
        headers: { "Content-Security-Policy": rendered[1] },
        body: rendered[0],
      });
    } else if (path.endsWith("/state")) {
      await route.fulfill({
        json: { can_comment: true, fresh_session_required: false, thread_href: "#/projects/p" },
      });
    } else if (path.endsWith("/comment-threads")) {
      await route.fulfill({ json: { threads: current } });
    } else if (path.endsWith(`/artifacts/${artifactId}/comments`)) {
      const body = request.postDataJSON();
      sends.push({ body, key: request.headers()["idempotency-key"] });
      const reply = answer(sends.length);
      if (reply.status === 200) {
        const sent = new Set(body.thread_ids);
        current = current.map((thread) =>
          sent.has(thread.thread_id) ? { ...thread, state: "resolved", can_send: false } : thread,
        );
      }
      await route.fulfill(reply);
    } else await route.fulfill({ status: 404 });
  });
  await page.goto(`${secureOrigin}/viewer`);
  return { page, errors, sends };
}

const sendButton = (page) => page.locator(".rcp-comments-footer button");
const sent = (page) => page.locator(".rcp-comments-sent");

test("Send posts the oldest 50 sendable threads once and links the chat", async () => {
  const browser = await chromium.launch();
  try {
    const list = Array.from({ length: 60 }, (_, index) => sendableThread(59 - index));
    const { page, errors, sends } = await openViewer(browser, list, () => ({
      status: 200,
      json: { operation_id: "op" },
    }));
    await page.evaluate(() => {
      window.posted = [];
      window.addEventListener("message", (event) => window.posted.push(event.data.type));
    });
    await sendButton(page).click();
    await sent(page).locator("a").click();
    await page.waitForFunction(() => window.posted.includes("rcp-artifact-open-chat"));
    assert.equal(sends.length, 1);
    assert.deepEqual(
      sends[0].body.thread_ids,
      Array.from({ length: 50 }, (_, index) => sendableThread(index).thread_id),
    );
    assert.ok(await page.evaluate(() => window.posted.includes("rcp-artifact-edit-started")));
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("a length refusal drops the newest thread from the next Send", async () => {
  const browser = await chromium.launch();
  try {
    const { page, errors, sends } = await openViewer(
      browser,
      [0, 1, 2].map((index) => sendableThread(index)),
      (count) =>
        count === 1
          ? { status: 400, json: { detail: { code: "comment_send_too_long", message: "Long" } } }
          : { status: 200, json: { operation_id: "op" } },
    );
    await sendButton(page).click();
    await page.locator(".rcp-comments-notice:not(:empty)").waitFor();
    await sendButton(page).click();
    await sent(page).waitFor();
    assert.deepEqual(
      sends.map((send) => send.body.thread_ids),
      [
        ["t000", "t001", "t002"],
        ["t000", "t001"],
      ],
    );
    assert.notEqual(sends[1].key, sends[0].key);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("the footer hides with nothing sendable, and a queued Send starts no edit", async () => {
  const browser = await chromium.launch();
  try {
    const idle = await openViewer(browser, [sendableThread(0, { can_send: false })], () => ({}));
    await idle.page.locator('[data-thread-id="t000"]').waitFor();
    assert.equal(await idle.page.locator(".rcp-comments-footer").count(), 0);
    const { page, errors } = await openViewer(browser, [sendableThread(0)], () => ({
      status: 200,
      json: { queued: true, queue_id: "q", operation_id: "op" },
    }));
    await page.evaluate(() => {
      window.posted = [];
      window.addEventListener("message", (event) => window.posted.push(event.data.type));
    });
    await sendButton(page).click();
    await sent(page).waitFor();
    await sendButton(page).waitFor({ state: "detached" });
    assert.ok(!(await page.evaluate(() => window.posted.includes("rcp-artifact-edit-started"))));
    assert.deepEqual([...idle.errors, ...errors], []);
  } finally {
    await browser.close();
  }
});
