import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { chromium, webkit } from "playwright";
import { createServer } from "vite";

const script = await readFile(
  new URL("../../src/rcp/artifact_selection.js", import.meta.url),
  "utf8",
);
const browserType = process.env.RCP_PREVIEW_BROWSER === "webkit" ? webkit : chromium;
const renderer = await readFile(new URL("../../src/rcp/artifacts.py", import.meta.url), "utf8");
const viewerScript = await readFile(
  new URL("../../src/rcp/artifact_viewer.js", import.meta.url),
  "utf8",
);
// Execute the actual renderer bridge, not a parallel implementation of its gates.
const bootstrap = renderer.match(
  /\+ _selection_script\(\)\s*\+ """([\s\S]*?)\}\)\(\);<\/script>"""/,
)[1];
const wrapper = renderer.match(
  /wrapper_script = """<script>\(\(\)=>\{([\s\S]*?)\}\)\(\);<\/script>"""/,
)[1];
const report = `<style>body{margin:24px;min-height:1200px;font:20px sans-serif}#figure{width:420px;height:210px;overflow:auto;background:#edf2f5}p{padding:18px}</style>
  <p id="text">Reference scores improve, but the scientific limitation remains.</p>
  <div id="figure"><svg width="420" height="420" aria-label="Validation scores"><rect x="30" y="30" width="80" height="120" fill="teal"/><text x="30" y="180">Validation</text></svg></div>
  <button id="control" onclick="this.textContent='Changed'">Report control</button>
  <details><summary>Details</summary>Expanded content</details>`;

async function drag(page, from, to) {
  await page.mouse.move(...from);
  await page.mouse.down();
  await page.mouse.move(...to, { steps: 8 });
  await page.mouse.up();
}

test("preview comments survive reopening and Open chat hands off the exact conversation", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  const browser = await browserType.launch();
  try {
    await server.listen();
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const context = await browser.newContext();
    const errors = [];
    const chatHash = "#/projects/project?view=chats&chat=originating-chat&branch_id=source-branch";
    await context.route(`${origin}/viewer*`, (route) => {
      const config = {
        projectId: "project",
        chatId: "originating-chat",
        chatAvailable: true,
        operationId: "operation",
        source: "task",
        episodeId: null,
        artifactId: new URL(route.request().url()).searchParams.get("artifact") || "a".repeat(24),
        artifactName: "plot.png",
        mediaType: "image/png",
        chatOpenTimeoutMs: 5000,
      };
      return route.fulfill({
        contentType: "text/html",
        body: `
        <meta charset="utf-8">
        <style>#boxLayer{position:absolute;inset:0 auto auto 0;width:600px;height:400px}aside{position:absolute;left:620px}</style>
        <div id="boxLayer"></div><aside>
        <section id="pending" hidden><div class="excerpt"></div><button data-confirm>Comment</button><button data-cancel>Cancel</button></section>
        <div id="items"></div><div id="empty"></div><button id="add" disabled>Add to chat</button>
        <a id="open-chat" href="/${chatHash}" hidden>Open chat</a><div id="notice"></div></aside>
        <script>(()=>{const config=${JSON.stringify(config)};${script}\n${viewerScript}})();</script>`,
      });
    });
    await context.route(`${origin}/receiver`, (route) =>
      route.fulfill({
        contentType: "text/html",
        body: "<title>RCP chat window</title>",
      }),
    );
    const reopen = async () => {
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${origin}/viewer`);
      return page;
    };
    let preview = await reopen();
    await drag(preview, [30, 30], [220, 160]);
    await preview.getByRole("button", { name: "Comment", exact: true }).click();
    const comment = "Why are these so different?\nCompare the two runs.";
    await preview.getByPlaceholder("Comment or question").fill(comment);
    await preview.close();
    preview = await reopen();
    assert.equal(await preview.getByPlaceholder("Comment or question").inputValue(), comment);
    assert.equal(await preview.getByRole("link", { name: "Open chat" }).isVisible(), false);
    await preview.getByRole("button", { name: "Add to chat" }).click();
    const payload = await preview.evaluate(() =>
      JSON.parse(localStorage.getItem("rcp:artifact-context:project:originating-chat")),
    );
    assert.equal(payload.selections[0].comment, comment);
    await preview.close();
    preview = await reopen();
    assert.equal(await preview.getByPlaceholder("Comment or question").inputValue(), comment);
    assert.equal(
      await preview.getByRole("link", { name: "Open chat" }).getAttribute("href"),
      `/${chatHash}`,
    );
    assert.equal(await preview.getByRole("link", { name: "Open chat" }).isVisible(), true);

    const main = await context.newPage();
    await main.goto(`${origin}/receiver`);
    await main.evaluate(async () => {
      const { listenForArtifactChatNavigation } = await import("/src/artifactChatNavigation.ts");
      listenForArtifactChatNavigation(async (hash) => {
        await new Promise((resolve, reject) => {
          window.finishNavigation = (error) => (error ? reject(new Error(error)) : resolve());
        });
        window.location.hash = hash;
      });
    });
    await preview.evaluate(() => {
      window.__TAURI_INTERNALS__ = {};
    });
    await preview.getByRole("link", { name: "Open chat" }).click();
    await main.waitForFunction(() => typeof window.finishNavigation === "function");
    assert.equal(await preview.locator("#notice").textContent(), "Opening chat…");
    assert.equal(main.url(), `${origin}/receiver`, "navigation waits for the conversation");
    await main.evaluate(() => window.finishNavigation("Conversation could not be opened."));
    await preview.getByText("Error: Conversation could not be opened.", { exact: true }).waitFor();
    await main.evaluate(() => {
      delete window.finishNavigation;
    });
    await preview.getByRole("link", { name: "Open chat" }).click();
    await main.waitForFunction(() => typeof window.finishNavigation === "function");
    await main.evaluate(() => window.finishNavigation());
    await main.waitForURL(`${origin}/receiver${chatHash}`);
    await preview.getByText("Opened the originating chat.").waitFor();
    assert.equal(preview.url(), `${origin}/viewer`, "desktop keeps the preview open");
    await preview.goto(`${origin}/viewer?artifact=${"b".repeat(24)}`);
    assert.equal(await preview.getByPlaceholder("Comment or question").count(), 0);
    await preview.goto(`${origin}/viewer`);
    assert.equal(await preview.getByPlaceholder("Comment or question").inputValue(), comment);
    await preview.getByRole("button", { name: "Remove selection 1" }).click();
    await preview.close();
    preview = await reopen();
    assert.equal(await preview.getByPlaceholder("Comment or question").count(), 0);
    assert.deepEqual(errors, []);
    await context.close();
  } finally {
    await browser.close();
    await server.close();
  }
});

test("only a confirmation-shell parent can enable HTML selection across opaque frames", async () => {
  const browser = await browserType.launch();
  try {
    for (const selectable of [false, true]) {
      const page = await browser.newPage({ viewport: { width: 1040, height: 760 } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.setContent(`<style>body{margin:0}iframe{width:700px;height:600px;border:0}aside{position:absolute;left:720px;top:0}</style>
        <iframe sandbox="allow-scripts"></iframe><aside><section id="pending" hidden><div class="excerpt"></div><button data-confirm>Comment</button><button data-cancel>Cancel</button></section></aside>`);
      await page.addScriptTag({ content: script });
      await page.evaluate((enabled) => {
        if (!enabled) return;
        const frame = document.querySelector("iframe");
        const offer = installSelectionConfirmation(
          document.querySelector("#pending"),
          () => {},
          () => {
            frame.contentWindow.postMessage({ type: "rcp-artifact-selection-clear" }, "*");
          },
        );
        window.addEventListener("message", (event) => {
          if (event.source === frame.contentWindow && event.data?.type === "rcp-artifact-selection")
            offer(event.data.selection);
        });
        frame.addEventListener("load", () => {
          frame.contentWindow.postMessage({ type: "rcp-artifact-selection-enable" }, "*");
        });
      }, selectable);
      // An artifact's ordinary window messages cannot opt itself in, even if it
      // knows the message names. The trusted bootstrap alone owns the private port.
      const inner = `<script>(()=>{${script}\n${bootstrap}})();</script>
        <script>parent.postMessage({type:'rcp-artifact-selection-enable'},'*');parent.postMessage({kind:'rcp-artifact-selection-enable'},'*');</script>${report}`;
      const escaped = inner
        .replaceAll("&", "&amp;")
        .replaceAll('"', "&quot;")
        .replaceAll("<", "&lt;");
      await page.locator("iframe").evaluate((frame, source) => {
        frame.srcdoc = source;
      }, `<style>body{margin:0}iframe{width:100%;height:600px;border:0}</style><script>(()=>{${wrapper}})();</script><iframe id="artifact" sandbox="allow-scripts" srcdoc="${escaped}"></iframe>`);
      const artifact = page.frameLocator("iframe").frameLocator("iframe");
      await artifact.locator("#figure").waitFor();
      await drag(page, [40, 160], [330, 300]);
      const outline = artifact.locator('[data-rcp-selection="area"]');
      if (selectable) {
        await page.locator("#pending").waitFor({ state: "visible" });
        assert.equal(await outline.count(), 1);
        await page.getByRole("button", { name: "Cancel", exact: true }).click();
        await outline.waitFor({ state: "detached" });
      } else {
        assert.equal(await outline.count(), 0);
        assert.equal(await page.locator("#pending").isVisible(), false);
        await artifact.locator("#control").click();
        assert.equal(await artifact.locator("#control").textContent(), "Changed");
      }
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await browser.close();
  }
});

test("direct preview drags require confirmation, preserve text, and keep working after cancel", async () => {
  const browser = await browserType.launch();
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    configFile: false,
    logLevel: "silent",
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { parseArtifactContextPayload } = await server.ssrLoadModule(
      "/src/components/NodeChat.tsx",
    );
    for (const kind of ["html", "image"]) {
      const page = await browser.newPage({ viewport: { width: 1040, height: 760 } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.setContent(`<style>body{margin:0}iframe,#image{width:700px;height:600px;border:0}#image{width:700.5px;height:600.25px;background:#edf2f5}aside{position:absolute;left:720px;top:10px}</style>
        ${kind === "html" ? '<iframe sandbox="allow-scripts"></iframe>' : '<div id="image"></div>'}
        <aside><section id="pending" hidden><div class="excerpt"></div><button data-confirm>Comment</button><button data-cancel>Cancel</button></section><div id="items"></div></aside>`);
      await page.addScriptTag({ content: script });
      await page.evaluate(() => {
        window.confirmed = [];
        let clearImage;
        const offer = installSelectionConfirmation(
          document.querySelector("#pending"),
          (selection) => {
            confirmed.push(selection);
            document.querySelector("#items").textContent = JSON.stringify(confirmed);
          },
          () => {
            if (frame) frame.contentWindow.postMessage("clear", "*");
            else clearImage();
          },
        );
        const frame = document.querySelector("iframe");
        if (frame)
          window.addEventListener("message", (event) => {
            if (event.source === frame.contentWindow) offer(event.data.selection);
          });
        else clearImage = installArtifactSelection(document.querySelector("#image"), offer);
      });
      if (kind === "html") {
        await page.locator("iframe").evaluate((frame, source) => {
          frame.srcdoc = source;
        }, `<script>${script}\nconst clear=installArtifactSelection(document,selection=>parent.postMessage({selection},'*'));window.addEventListener('message',e=>{if(e.source===parent&&e.data==='clear')clear()});</script>${report}`);
        await page.frameLocator("iframe").locator("#figure").waitFor();
      }
      const pending = page.locator("#pending");
      const outline =
        kind === "html"
          ? page.frameLocator("iframe").locator('[data-rcp-selection="area"]')
          : page.locator('[data-rcp-selection="area"]');
      const origin = kind === "html" ? [40, 160] : [20, 40];
      const destination = kind === "html" ? [330, 300] : [350, 250];

      await page.mouse.click(...origin);
      assert.equal(await pending.isHidden(), true, "a click is not a selection");
      await drag(page, origin, destination);
      await pending.waitFor({ state: "visible" });
      assert.equal(await outline.count(), 1, "keep the area visible during confirmation");
      assert.deepEqual(await page.evaluate(() => confirmed), [], "a drag is not a capture");
      const originalOutline = await outline.boundingBox();
      await page.mouse.click(...origin);
      await drag(page, origin, [origin[0] + 2, origin[1] + 2]);
      assert.equal(await pending.isVisible(), true, "clicks do not dismiss confirmation");
      assert.deepEqual(await outline.boundingBox(), originalOutline);
      const gestureSurface =
        kind === "html" ? page.frameLocator("iframe").locator("body") : page.locator("#image");
      // Native touch scrolling cancels its pointer; that ignored pointer does
      // not own the already pending mouse selection.
      await gestureSurface.dispatchEvent("pointercancel", {
        pointerId: 1234,
        pointerType: "touch",
      });
      assert.equal(await pending.isVisible(), true);
      assert.deepEqual(await outline.boundingBox(), originalOutline);
      if (kind === "html") {
        const content = page.frameLocator("iframe");
        const artifactFrame = page.frames().find((frame) => frame.parentFrame());
        const waitForTop = (top) =>
          artifactFrame.waitForFunction(
            (expected) =>
              Math.abs(
                document.querySelector('[data-rcp-selection="area"]').getBoundingClientRect().top -
                  expected,
              ) < 1,
            top,
          );
        await content.locator("body").evaluate(() => window.scrollTo(0, 40));
        await waitForTop(originalOutline.y - 40);
        assert.equal(await pending.isVisible(), true, "page scroll preserves pending area");
        await content.locator("body").evaluate(() => window.scrollTo(0, 0));
        await waitForTop(originalOutline.y);
        await content.locator("#figure").evaluate((figure) => {
          figure.scrollTop = 40;
        });
        await waitForTop(originalOutline.y - 40);
        assert.equal(await pending.isVisible(), true, "nested scroll preserves pending area");
        await content.locator("#figure").evaluate((figure) => {
          figure.scrollTop = 0;
        });
        await waitForTop(originalOutline.y);
      }
      await page.getByRole("button", { name: "Cancel", exact: true }).click();
      await outline.waitFor({ state: "detached" });
      assert.equal(await pending.isHidden(), true);
      assert.deepEqual(await page.evaluate(() => confirmed), []);

      await drag(page, destination, origin);
      await pending.waitFor({ state: "visible" });
      await page.getByRole("button", { name: "Comment", exact: true }).click();
      await outline.waitFor({ state: "detached" });
      assert.equal(await pending.isHidden(), true);
      const [selection] = await page.evaluate(() => confirmed);
      assert.equal(selection.kind, "box");
      assert.ok(selection.rect.width > 0 && selection.rect.height > 0);
      assert.ok(selection.rect.x >= 0 && selection.rect.x + selection.rect.width <= 1);
      assert.ok(selection.rect.y >= 0 && selection.rect.y + selection.rect.height <= 1);
      if (kind === "image") {
        const area = await page.locator("#image").boundingBox();
        assert.equal(Number.isInteger(area.width), false);
        assert.equal(Number.isInteger(area.height), false);
        assert.equal(selection.rect.width, Math.abs(destination[0] - origin[0]) / area.width);
        assert.equal(selection.rect.height, Math.abs(destination[1] - origin[1]) / area.height);
        const payload = {
          type: "rcp-artifact-context",
          version: 1,
          project_id: "project",
          chat_id: "chat",
          operation_id: "operation",
          artifact_id: "0123456789abcdef01234567",
          artifact_name: "plot.png",
          media_type: "image/png",
          selections: [{ ...selection, comment: "Compare this region." }],
        };
        assert.deepEqual(parseArtifactContextPayload(payload), payload);
      }

      // A later area needs no re-arming, and Escape cancels without a stale box.
      await drag(page, origin, destination);
      await pending.waitFor({ state: "visible" });
      await page.keyboard.press("Escape");
      await pending.waitFor({ state: "hidden" });
      await outline.waitFor({ state: "detached" });
      // A preceding confirmation can leave keyboard focus in the outer shell.
      // Beginning a new area must direct Escape to the frame owning the drag,
      // rather than racing a parent clear message against pointerup.
      await page.locator("aside").evaluate((rail) => {
        rail.tabIndex = -1;
        rail.focus();
      });
      await page.mouse.move(...origin);
      await page.mouse.down();
      await page.mouse.move(...destination, { steps: 5 });
      if (kind === "html")
        assert.equal(await page.frames()[1].evaluate(() => document.hasFocus()), true);
      await page.keyboard.press("Escape");
      assert.equal(await outline.count(), 0);
      await page.mouse.up();
      await pending.waitFor({ state: "hidden" });
      assert.equal(await page.evaluate(() => confirmed.length), 1);

      if (kind === "html") {
        const content = page.frameLocator("iframe");
        const positions = await content.locator("#text").evaluate((paragraph) => {
          const text = paragraph.firstChild;
          const range = document.createRange();
          range.setStart(text, 0);
          range.setEnd(text, 1);
          const first = range.getBoundingClientRect();
          range.setStart(text, 16);
          range.setEnd(text, 17);
          const last = range.getBoundingClientRect();
          return {
            from: [first.left + 1, first.top + first.height / 2],
            to: [last.right - 1, last.top + last.height / 2],
          };
        });
        await drag(page, positions.from, positions.to);
        await pending.waitFor({ state: "visible" });
        await page.waitForFunction(() =>
          document.querySelector("#pending .excerpt").textContent.includes("Reference"),
        );
        assert.match(
          await content.locator("body").evaluate(() => document.getSelection()?.toString()),
          /Reference/,
        );
        assert.equal(await page.evaluate(() => confirmed.length), 1);
        await page.getByRole("button", { name: "Comment", exact: true }).click();
        assert.equal(await page.evaluate(() => confirmed[1].kind), "text");
        await content.getByRole("button", { name: "Report control" }).click();
        assert.equal(await content.locator("#control").innerText(), "Changed");
        await content.locator("summary").click();
        assert.equal(await content.locator("details").getAttribute("open"), "");
        assert.equal(await page.evaluate(() => confirmed.length), 2);
      }
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await server.close();
    await browser.close();
  }
});
