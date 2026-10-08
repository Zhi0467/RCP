import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";
import { chromium } from "playwright";

// The real shell and content, rendered by the backend exactly as the routes serve them.
const rendered = JSON.parse(
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `import json
from rcp.artifacts import AgentArtifactDescriptor
from rcp.artifact_comments import selection_frame_addon
from rcp.artifact_views import InlineAppearance, artifact_content, artifact_viewer_document
page = b"""<!doctype html><html><body><style>:root{color-scheme:light dark}body{margin:0}.box{height:500px;width:100%}</style>
<div class=box id=box></div><button id=grow onclick="box.style.height='700px'">grow</button>
<p id=ink style="color:var(--rcp-ink)">ink</p></body></html>"""
d = AgentArtifactDescriptor(artifact_id="a"*24, name="page.html", media_type="text/html", size_bytes=1)
shell, _ = artifact_viewer_document(d, content_url="/content?version_id=v1", state="temporary", presentation="inline", selectable=True)
content, _, csp = artifact_content("page.html", "text/html", page, frame_addon=selection_frame_addon(), inline=InlineAppearance("aqua", "dark"))
print(json.dumps({"shell": shell, "content": content}))`,
    ],
    { cwd: new URL("../..", import.meta.url), encoding: "utf8" },
  )
    .trim()
    .split("\n")
    .pop(),
);

test("an inline artifact sizes to its content, blends in dark mode, and selects only in comment mode", async () => {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ colorScheme: "light" });
    const contentRequests = [];
    await page.route("http://rcp.test/app", (route) =>
      route.fulfill({
        contentType: "text/html",
        body: `<html data-theme="aqua" data-color-mode="dark" style="color-scheme:dark;background:#282e36">
<body style="margin:0"><iframe id="embed" src="/viewer" style="border:0;width:600px;height:320px"></iframe>
<script>window.received=[];addEventListener("message",(e)=>{if(e.source===embed.contentWindow&&e.origin===location.origin)received.push(e.data)})</script></body></html>`,
      }),
    );
    await page.route("http://rcp.test/viewer", (route) =>
      route.fulfill({ contentType: "text/html", body: rendered.shell }),
    );
    await page.route("http://rcp.test/content?**", (route) => {
      contentRequests.push(new URL(route.request().url()).searchParams);
      return route.fulfill({ contentType: "text/html", body: rendered.content });
    });
    await page.goto("http://rcp.test/app");
    const sizes = () =>
      page.evaluate(() =>
        window.received.filter((m) => m.type === "rcp-artifact-size").map((m) => m.height),
      );
    await page.waitForFunction(() =>
      window.received.some((m) => m.type === "rcp-artifact-size" && m.height > 500),
    );
    // The shell asked for content in the reply's appearance.
    assert.equal(contentRequests[0].get("presentation"), "inline");
    assert.equal(contentRequests[0].get("color_mode"), "dark");
    assert.equal(contentRequests[0].get("version_id"), "v1");
    const settled = (await sizes()).at(-1);
    assert.ok(settled >= 520 && settled < 600, `content height ${settled}`);

    const shell = page.frame({ url: "http://rcp.test/viewer" });
    const wrapper = shell.childFrames()[0];
    const artifact = wrapper.childFrames()[0];
    await artifact.locator("#grow").click();
    await page.waitForFunction(() =>
      window.received.some((m) => m.type === "rcp-artifact-size" && m.height > 700),
    );

    // Every frame declares the reply's scheme, so the nested page stays transparent,
    // even though the page itself asked for "light dark" (the OS preference here is light).
    assert.equal(
      await artifact.evaluate(() => getComputedStyle(document.documentElement).colorScheme),
      "dark",
    );
    assert.equal(
      await artifact.evaluate(() => getComputedStyle(document.getElementById("ink")).color),
      "rgb(229, 235, 242)",
    );
    assert.equal(
      await artifact.evaluate(() => getComputedStyle(document.body).backgroundColor),
      "rgba(0, 0, 0, 0)",
    );
    assert.equal(
      await wrapper.evaluate(() => getComputedStyle(document.documentElement).colorScheme),
      "dark",
    );

    // A drag selects nothing until the chat turns comment mode on.
    const box = await page.locator("#embed").boundingBox();
    const drag = async () => {
      await page.mouse.move(box.x + 40, box.y + 40);
      await page.mouse.down();
      await page.mouse.move(box.x + 240, box.y + 200, { steps: 6 });
      await page.mouse.up();
      await page.waitForTimeout(200);
    };
    const selections = () =>
      page.evaluate(() =>
        window.received.filter((m) => m.type === "rcp-artifact-selection" && m.selection),
      );
    await drag();
    assert.equal((await selections()).length, 0);
    await page.evaluate(() =>
      document
        .getElementById("embed")
        .contentWindow.postMessage(
          { type: "rcp-inline-comment-mode", version: 1, enabled: true },
          location.origin,
        ),
    );
    await page.waitForTimeout(200);
    await drag();
    const [selection] = await selections();
    assert.equal(selection?.selection?.kind, "box");
    assert.ok(selection.description.length > 0);
    await page.evaluate(() =>
      document
        .getElementById("embed")
        .contentWindow.postMessage(
          { type: "rcp-inline-comment-mode", version: 1, enabled: false },
          location.origin,
        ),
    );
    await page.waitForTimeout(200);
    await drag();
    assert.equal((await selections()).length, 1);
  } finally {
    await browser.close();
  }
});
