import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("the tab strip keeps View and the branch picker inside its visible width", async () => {
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
      await page.goto(`${base}/projectTabStrip.html`);
      const strip = page.locator(".project-tabs");
      await page.locator(".branch-picker").waitFor();
      const visible = () =>
        strip.evaluate((nav) => {
          const edge = nav.getBoundingClientRect().left + nav.clientLeft + nav.clientWidth;
          const right = (selector) => nav.querySelector(selector).getBoundingClientRect().right;
          return {
            overflows: nav.scrollWidth > nav.clientWidth,
            picker: right(".branch-picker") <= edge,
            view: right(".view-picker") <= edge,
          };
        });
      // The strip overflows; the trailing controls stay pinned wherever it is scrolled.
      assert.deepEqual(
        await visible(),
        { overflows: true, picker: true, view: true },
        `${width}px`,
      );
      await strip.evaluate((nav) => (nav.scrollLeft = nav.scrollWidth));
      assert.deepEqual(
        await visible(),
        { overflows: true, picker: true, view: true },
        `${width}px`,
      );
      assert.deepEqual(errors, []);
      await page.close();
    }
  } finally {
    await browser?.close();
    await server.close();
  }
});
