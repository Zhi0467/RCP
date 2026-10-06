import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

// Elements that scroll horizontally, other than the boxes meant to: code
// blocks, tables, display math, and the DAG canvas.
function sidewaysScrollers() {
  return [...document.querySelectorAll("body *")]
    .filter((element) => {
      if (element.closest("pre, table, .katex-display, .dag-scroll")) return false;
      const { overflowX } = getComputedStyle(element);
      return (
        ["auto", "scroll", "hidden"].includes(overflowX) &&
        element.scrollWidth > element.clientWidth + 1 &&
        !element.matches("[style*='text-overflow'], .ellipsis")
      );
    })
    .map(
      (element) =>
        `${element.className || element.tagName}: ${element.scrollWidth}>${element.clientWidth}`,
    );
}

test("narrow Chats and DAG keep the working surface primary behind accessible disclosures", async (t) => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const base = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures`;
    for (const viewport of [
      { width: 390, height: 844 },
      { width: 360, height: 780 },
    ]) {
      const page = await browser.newPage({ viewport, reducedMotion: "reduce" });
      await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      page.on("requestfailed", (request) => errors.push(request.url()));
      await page.route("**/api/projects/project/chats/*/worktree**", (route) =>
        route.fulfill({ json: { show_chooser: false, binding: null, integration_options: [] } }),
      );
      await page.route("**/api/projects/*/chats/*/questions", (route) =>
        route.fulfill({ json: [] }),
      );
      await page.route("**/api/projects/*/chats/*/browser", (route) =>
        route.fulfill({ json: { browser_requested: false } }),
      );
      await page.route("**/api/projects/*/chat-display", (route) =>
        route.fulfill({ json: { archived: [], titles: {}, pinned: [] } }),
      );
      await page.route("**/api/projects/fixture/graph-edit-options", (route) =>
        route.fulfill({ json: { node_prefixes: {}, relations: [] } }),
      );
      await page.route("**/api/service-connections", (route) =>
        route.fulfill({ json: { dictation: "system", connections: [] } }),
      );

      await page.goto(`${base}/mobileChats.html`);
      const composer = page.getByRole("textbox", { name: "Message", exact: true });
      await composer.waitFor();
      const list = page.locator(".conversation-list");
      const chatToggle = page.getByRole("button", { name: "Agents", exact: true });
      assert.equal(await list.isVisible(), false, "Mobile conversation list starts closed");
      assert.equal(await page.getByRole("separator").count(), 0, "No narrow resize strip");
      const composerWidth = (await composer.boundingBox()).width;
      assert.ok(composerWidth >= viewport.width - 50, "Composer uses the full narrow column");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), viewport.width);
      await page.locator(".chat-markdown table").waitFor();
      assert.deepEqual(
        await page.evaluate(sidewaysScrollers),
        [],
        "Long agent tokens wrap; only code blocks and tables scroll sideways",
      );
      const copy = page.getByRole("button", { name: "Copy code block" });
      // A small control whose invisible margin still makes a 44px tap target.
      const copyBox = await copy.boundingBox();
      assert.ok(copyBox.width <= 32, `The copy control stays small, got ${copyBox.width}px`);
      assert.ok(
        await page.evaluate(({ x, y, width }) => {
          const outside = document.elementFromPoint(x - 6, y + 2);
          return outside?.closest(".markdown-code-copy") !== null && width + 16 >= 44;
        }, copyBox),
        "A tap just outside the visible control still reaches it",
      );
      await copy.click();
      await page.getByRole("button", { name: "Copied" }).waitFor();
      assert.equal(
        await page.evaluate(() => navigator.clipboard.readText()),
        "python extract_probes.py --input outputs/model_stats/main_rollout_stats_rebuild_20260424_lcb_first_dp4 --output outputs/model_stats/main_rollout_stats_rebuild_20260424_lcb_first_dp4_probes",
        "Copy takes the block's exact text, without a trailing newline",
      );
      // A glossary definition opened from a term near the right edge stays on
      // screen and does not widen the transcript.
      const term = page.locator(".chat-annotatable-answer .glossary-definition").first();
      await term.focus();
      const definition = await term.evaluate((element) => {
        const style = getComputedStyle(element, "::after");
        return { display: style.display, position: style.position };
      });
      assert.deepEqual(await page.evaluate(sidewaysScrollers), []);
      assert.deepEqual(definition, { display: "block", position: "fixed" });
      await term.blur();
      // A term in a node-detail header, whose desktop rule anchors below the
      // header, uses the same on-screen strip.
      const headerDefinition = await page.evaluate(() => {
        const drawer = document.createElement("aside");
        drawer.className = "detail-drawer node-detail-drawer";
        drawer.innerHTML =
          '<header><div><h2><dfn class="glossary-definition" tabindex="0" data-definition="A long definition for a header term.">schema</dfn></h2></div></header>';
        document.body.append(drawer);
        const dfn = drawer.querySelector("dfn");
        dfn.focus();
        const style = getComputedStyle(dfn, "::after");
        const result = {
          position: style.position,
          onScreen: parseFloat(style.top) >= 0 && parseFloat(style.bottom) >= 0,
        };
        drawer.remove();
        return result;
      });
      assert.deepEqual(headerDefinition, { position: "fixed", onScreen: true });

      // Touch selection settles without a pointer release: the offer follows the
      // selection itself, and nothing opens until the reader chooses Comment. The
      // selection ends inside a link: showing the offer re-renders the answer,
      // and the reader's selection must survive that unchanged to stay copyable.
      const selected = await page.evaluate(() => {
        const paragraph = document.querySelector(".chat-annotatable-answer p");
        const range = document.createRange();
        range.setStart(paragraph.firstChild, 4);
        range.setEnd(paragraph.querySelector("a").firstChild, 20);
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
        return window.getSelection().toString();
      });
      const offer = page.getByRole("button", { name: "Comment", exact: true });
      await offer.waitFor();
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
      assert.equal(await page.evaluate(() => window.getSelection().toString()), selected);
      assert.equal(
        await page.evaluate(() => document.activeElement.matches("input, textarea")),
        false,
        "No field takes focus, which would clear a touch selection",
      );
      const offerBox = await offer.boundingBox();
      assert.ok(offerBox.height <= 32, `Comment stays compact, got ${offerBox.height}px`);
      assert.ok(
        await page.evaluate(
          ({ x, y, width }) =>
            document.elementFromPoint(x + width / 2, y - 4)?.closest(".chat-selection-comment") !==
            null,
          offerBox,
        ),
        "A tap just outside Comment still reaches it",
      );
      assert.ok(offerBox.x >= 0 && offerBox.x + offerBox.width <= viewport.width);
      assert.equal(await page.getByRole("form", { name: "Add annotation" }).count(), 0);
      await page.evaluate(() => window.getSelection().removeAllRanges());
      await offer.waitFor({ state: "detached" });
      await composer.fill("A draft survives opening the conversation list.");
      await chatToggle.focus();
      await page.keyboard.press("Enter");
      await list.waitFor();
      assert.equal(await composer.inputValue(), "A draft survives opening the conversation list.");
      assert.equal((await composer.boundingBox()).width, composerWidth);
      assert.ok(
        (await composer.evaluate((element) => parseFloat(getComputedStyle(element).fontSize))) >=
          16,
        "A focused field is at least 16px, so iOS does not zoom the page",
      );
      for (const height of await list
        .locator('[role="option"], input[type="search"], .agent-list-filters button')
        .evaluateAll((controls) =>
          controls.map((control) => control.getBoundingClientRect().height),
        )) {
        assert.ok(height >= 44, `Panel controls are at least 44px tall, got ${height}`);
      }
      await page.getByRole("option", { name: "Second chat, project conversation" }).click();
      await list.waitFor({ state: "hidden" });
      await chatToggle.click();
      assert.equal(
        await page
          .getByRole("option", { name: "Second chat, project conversation" })
          .getAttribute("aria-selected"),
        "true",
      );
      await chatToggle.click();
      await list.waitFor({ state: "hidden" });

      await page.setViewportSize({ width: 1100, height: 900 });
      await list.waitFor();
      const resize = page.getByRole("separator", { name: "Resize conversation list" });
      const widthBeforeResize = Number(await resize.getAttribute("aria-valuenow"));
      await resize.focus();
      await page.keyboard.press("ArrowRight");
      await page.waitForFunction(
        (width) =>
          Number(
            document.querySelector(".conversation-resize-handle").getAttribute("aria-valuenow"),
          ) > width,
        widthBeforeResize,
      );
      const desktopWidth = Number(await resize.getAttribute("aria-valuenow"));
      await page.waitForFunction(
        (width) =>
          document.querySelector(".conversation-list").getBoundingClientRect().width === width,
        desktopWidth,
      );
      await page.getByRole("button", { name: "Collapse conversation list" }).click();
      await list.waitFor({ state: "hidden" });
      await page.setViewportSize(viewport);
      await chatToggle.waitFor();
      await chatToggle.click();
      await list.waitFor();
      await page.setViewportSize({ width: 1100, height: 900 });
      await list.waitFor({ state: "hidden" });
      await page.getByRole("button", { name: "Expand conversation list" }).click();
      await list.waitFor();
      assert.equal(
        await page
          .locator(".conversation-list")
          .evaluate((element) => element.getBoundingClientRect().width),
        desktopWidth,
        "Narrow disclosure does not overwrite the desktop resize preference",
      );

      await page.setViewportSize(viewport);
      await page.goto(`${base}/dagViewport.html?representative`);
      await page.locator(".dag-node").last().waitFor();
      const dagToggle = page.locator(".dag-controls-disclosure > summary");
      const controls = page.locator(".dag-controls");
      assert.equal(await controls.isVisible(), false, "DAG controls start closed on mobile");
      const canvasTop = (await page.locator(".dag-scroll").boundingBox()).y;
      assert.ok(canvasTop < viewport.height / 3, "Canvas begins in the top third of the fixture");
      await dagToggle.focus();
      await page.keyboard.press("Enter");
      await controls.waitFor();
      await page.getByRole("button", { name: "Research flow", exact: true }).click();
      assert.equal(
        await page
          .getByRole("button", { name: "Research flow", exact: true })
          .getAttribute("aria-pressed"),
        "true",
      );
      await dagToggle.click();
      await controls.waitFor({ state: "hidden" });
      assert.equal((await page.locator(".dag-scroll").boundingBox()).y, canvasTop);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), viewport.width);
      await page.setViewportSize({ width: 1100, height: 900 });
      await controls.waitFor();
      assert.equal(await dagToggle.isVisible(), false, "Desktop controls remain directly visible");
      assert.deepEqual(errors, []);
      t.diagnostic(
        `${viewport.width}x${viewport.height}: composer ${composerWidth}px; canvas top ${canvasTop}px (component fixtures).`,
      );
      await page.close();
    }
  } finally {
    await browser?.close();
    await server.close();
  }
});
