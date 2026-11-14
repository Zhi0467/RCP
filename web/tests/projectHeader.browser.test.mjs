import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("the one-row header fits its controls and its panel picker drops above the page", async () => {
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
    for (const width of [1050, 768]) {
      const page = await browser.newPage({ viewport: { width, height: 700 } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`${base}/projectHeader.html`);
      const header = page.locator(".project-header");
      await page.locator(".branch-picker").waitFor();
      const fit = await header.evaluate((element) => {
        const box = element.getBoundingClientRect();
        const inside = (selector) => {
          const rect = element.querySelector(selector).getBoundingClientRect();
          return rect.top >= box.top && rect.bottom <= box.bottom && rect.right <= box.right;
        };
        return {
          oneRow: box.height <= 56,
          picker: inside(".branch-picker"),
          view: inside(".view-picker"),
          newTab: inside(".project-dock-new"),
        };
      });
      assert.deepEqual(fit, { oneRow: true, picker: true, view: true, newTab: true }, `${width}px`);

      const trigger = page.locator(".panel-picker-trigger");
      const menu = page.locator(".panel-picker-menu");
      await trigger.hover();
      const runs = menu.getByRole("button", { name: "Runs", exact: true });
      await runs.waitFor();
      assert.equal(await runs.getAttribute("aria-current"), "page");
      const box = await menu.boundingBox();
      assert.ok(
        await menu.evaluate(
          (element, [x, y]) => element.contains(document.elementFromPoint(x, y)),
          [box.x + box.width / 2, box.y + box.height - 4],
        ),
        `${width}px`,
      );
      await menu.getByRole("button", { name: "Inbox", exact: true }).click();
      assert.equal(await trigger.getAttribute("aria-label"), "Panel: Inbox");
      assert.equal(await trigger.getAttribute("aria-expanded"), "false");

      await page.mouse.move(width - 5, 600);
      await trigger.focus();
      await page.keyboard.press("Enter");
      assert.equal(await trigger.getAttribute("aria-expanded"), "true");
      await page.keyboard.press("Escape");
      assert.equal(await trigger.getAttribute("aria-expanded"), "false");
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await browser?.close();
    await server.close();
  }
});
