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

const errorPages = JSON.parse(
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `
import json
from rcp.artifact_comments import comment_panel
from rcp.artifact_views import artifact_content, artifact_viewer_document
from rcp.artifacts import AgentArtifactDescriptor, FrameAddon
from rcp.limits import ARTIFACT_ERROR_MAX_CHARS
d = AgentArtifactDescriptor(artifact_id="a"*24, name="page.html", media_type="text/html", size_bytes=1)
result = {"limit": ARTIFACT_ERROR_MAX_CHARS}
for mode in ("panel", "inline"):
    result[mode] = artifact_viewer_document(d, content_url="/content", state="temporary", keep_url="/keep", presentation=mode, selectable=True, panel=comment_panel({"projectId":"p", "artifactId":d.artifact_id, "mediaType":"text/html"}))[0]
for name, script in {"sync":"throw new Error('broken <img src=x>')", "quiet":"", "reject":"Promise.reject('rejected')", "long":"throw new Error('x'.repeat(10000))"}.items():
    addon = FrameAddon(frame_script="send({kind:'rcp-artifact-error',message:'extra-key',count:1,extra:1});", wrapper_script="") if name == "quiet" else None
    result[name] = artifact_content("page.html", "text/html", ("<!doctype html><p id=drawn>drawn</p><script>"+script+"</script>").encode(), frame_addon=addon)[0]
print(json.dumps(result))`,
    ],
    { cwd: new URL("../..", import.meta.url), encoding: "utf8" },
  )
    .trim()
    .split("\n")
    .pop(),
);

async function errorPage(t, mode, content) {
  const browser = await chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage();
  const posts = [];
  await page.route("http://rcp.test/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "POST") posts.push(path);
    if (path.endsWith("/state")) return route.fulfill({ json: { can_comment: true } });
    const body =
      path === "/app"
        ? `<iframe id=embed src=/shell></iframe><a download href=/download></a><script>window.received=[];addEventListener('message',e=>received.push(e.data))</script>`
        : path === "/shell"
          ? errorPages[mode]
          : errorPages[content];
    return route.fulfill({ contentType: "text/html", body });
  });
  await page.goto("http://rcp.test/app");
  const shell = page.frame({ url: "http://rcp.test/shell" });
  const wrapper = shell.childFrames()[0],
    artifact = wrapper.childFrames()[0];
  return { page, shell, wrapper, artifact, posts };
}

test("script errors preserve the drawing and actions and prefill only a human comment", async (t) => {
  const { page, shell, artifact, posts } = await errorPage(t, "panel", "sync");
  await shell.locator('#pageError[data-count="1"]').waitFor();
  assert.ok(
    (await shell.locator("#pageErrorMessage").textContent()).endsWith("broken <img src=x>"),
  );
  assert.equal(await shell.locator("#pageError img").count(), 0);
  assert.equal(await artifact.locator("#drawn").count(), 1);
  assert.equal(await page.locator("a[download]").count(), 1);
  assert.equal(await shell.locator("#keep").isVisible(), true);
  await shell.locator("#message").evaluate((field) => (field.value = "my draft"));
  await shell.locator("#askFix").click();
  const draft = await shell.locator("#message").inputValue();
  assert.ok(draft.startsWith("my draft") && draft.endsWith("broken <img src=x>"));
  await shell.locator("#askFix").click();
  assert.equal(await shell.locator("#message").inputValue(), draft);
  assert.deepEqual(posts, []);
});

test("rejections count and a flood coalesces into bounded updates", async (t) => {
  const { page, artifact } = await errorPage(t, "inline", "reject");
  await page.waitForFunction(() => received.some((v) => v.kind === "rcp-artifact-error"));
  await artifact.evaluate(() => {
    for (let i = 0; i < 100; i++)
      dispatchEvent(new ErrorEvent("error", { error: new Error("flood") }));
  });
  await page.waitForFunction(() => received.some((v) => v.count === 101));
  const errors = await page.evaluate(() => received.filter((v) => v.kind === "rcp-artifact-error"));
  assert.equal(errors[0].count, 1);
  assert.equal(errors.at(-1).count, 101);
  assert.ok(errors.length <= 3);
  assert.equal(errors[0].message, "rejected");
});

test("direct page forgeries and extra keys never reach the caption", async (t) => {
  const { page, wrapper, artifact } = await errorPage(t, "inline", "quiet");
  await artifact.evaluate(() => {
    parent.postMessage({ kind: "rcp-artifact-error", message: "forged", count: 1 }, "*");
    parent.parent.postMessage({ kind: "rcp-artifact-error", message: "forged", count: 1 }, "*");
  });
  await wrapper.evaluate(() => {
    const channel = new URL(location.href).searchParams.get("error_channel");
    parent.postMessage(
      { kind: "rcp-artifact-error", message: "forged", count: 1, channel, extra: 1 },
      "*",
    );
    parent.postMessage(
      { kind: "rcp-artifact-error", message: "stale", count: 1, channel: "old" },
      "*",
    );
    parent.postMessage({ marker: true }, "*");
  });
  // A following legitimate summary acts as the message-queue fence.
  await artifact.evaluate(() =>
    dispatchEvent(new ErrorEvent("error", { error: new Error("real") })),
  );
  await page.waitForFunction(() => received.some((v) => v.kind === "rcp-artifact-error"));
  assert.deepEqual(
    await page.evaluate(() =>
      received.filter((v) => v.kind === "rcp-artifact-error").map((v) => v.message),
    ),
    ["real"],
  );
});

test("error strings are truncated at the server bound without serializing objects", async (t) => {
  const { page, artifact } = await errorPage(t, "inline", "long");
  await page.waitForFunction(() => received.some((v) => v.kind === "rcp-artifact-error"));
  assert.equal(
    await page.evaluate(() => received.find((v) => v.kind === "rcp-artifact-error").message.length),
    errorPages.limit,
  );
  await artifact.evaluate(() => {
    Promise.reject({
      toString() {
        window.serialized = true;
        return "secret";
      },
    });
  });
  await page.waitForFunction(() => received.some((v) => v.count === 2));
  assert.equal(await artifact.evaluate(() => window.serialized), undefined);
  assert.equal(
    await page.evaluate(() => typeof received.find((v) => v.count === 2).message),
    "string",
  );
});

test("a content reload clears the old notice and rejects the previous generation", async (t) => {
  const { page, shell, wrapper } = await errorPage(t, "inline", "sync");
  await page.waitForFunction(() => received.some((v) => v.kind === "rcp-artifact-error"));
  await page.route("http://rcp.test/content?**", (route) =>
    route.fulfill({ contentType: "text/html", body: errorPages.quiet }),
  );
  const previous = new URL(wrapper.url()).searchParams.get("error_channel");
  await page.evaluate(() => {
    received.length = 0;
    document.documentElement.dataset.theme = "classic";
  });
  await page.waitForFunction(() => received.some((v) => v.kind === "rcp-artifact-error-clear"));
  await shell.waitForFunction(
    (old) =>
      new URL(document.getElementById("preview").src).searchParams.get("error_channel") !== old,
    previous,
  );
  await shell
    .childFrames()[0]
    .evaluate(
      (channel) =>
        parent.postMessage(
          { kind: "rcp-artifact-error", message: "stale", count: 1, channel },
          "*",
        ),
      previous,
    );
  await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 50)));
  assert.equal(
    await page.evaluate(() => received.filter((v) => v.kind === "rcp-artifact-error").length),
    0,
  );
});
