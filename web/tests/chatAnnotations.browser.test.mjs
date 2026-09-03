import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright-core";
import { createServer } from "vite";

test("a wide annotation composer stays interactive inside a keyboard-shrunken visual viewport", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  try {
    await server.listen();
    const address = server.httpServer?.address();
    assert.ok(address && typeof address === "object");
    browser = await chromium.launch({ channel: "chrome", headless: true });
    const page = await browser.newPage({ viewport: { width: 844, height: 520 } });
    await page.addInitScript(() => {
      class TestVisualViewport extends EventTarget {
        width = window.innerWidth;
        height = window.innerHeight;
        offsetLeft = 0;
        offsetTop = 0;

        shrink(height, offsetTop) {
          this.height = height;
          this.offsetTop = offsetTop;
          this.dispatchEvent(new Event("resize"));
        }
      }
      const viewport = new TestVisualViewport();
      Object.defineProperty(window, "visualViewport", {
        configurable: true,
        value: viewport,
      });
      window.shrinkTestVisualViewport = (height, offsetTop) => viewport.shrink(height, offsetTop);
    });

    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/chatAnnotationViewport.html`);
    const comment = page.getByRole("button", { name: "Comment on this answer" });
    await comment.waitFor({ state: "visible" });
    assert.equal(await page.evaluate(() => window.innerWidth), 844);
    await comment.click();
    const composer = page.getByRole("form", { name: "Select answer text" });
    await composer.waitFor({ state: "visible" });

    await page.evaluate(() => window.shrinkTestVisualViewport(196, 48));
    await page.waitForFunction(() => {
      const element = document.querySelector(".chat-annotation-composer");
      return element instanceof HTMLElement && element.getBoundingClientRect().bottom <= 232;
    });

    const layout = await composer.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return {
        top: rect.top,
        bottom: rect.bottom,
        width: rect.width,
        clientHeight: element.clientHeight,
        scrollHeight: element.scrollHeight,
        overflowY: style.overflowY,
      };
    });
    assert.ok(layout.top >= 60, `composer top ${layout.top} escaped the visual viewport`);
    assert.ok(layout.bottom <= 232, `composer bottom ${layout.bottom} escaped the visual viewport`);
    assert.equal(layout.width, 320);
    assert.equal(layout.overflowY, "auto");
    assert.ok(layout.scrollHeight > layout.clientHeight, "constrained composer should scroll");

    await page.getByRole("button", { name: "Cancel annotation" }).click();
    await composer.waitFor({ state: "detached" });
  } finally {
    await browser?.close();
    await server.close();
  }
});
