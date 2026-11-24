import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

const artifacts = ["report", "plot", "table"].map((artifact_id) => ({
  artifact_id,
  name: `${artifact_id}.html`,
  media_type: "text/html",
  view: "html",
  supplier: "turn",
  origin_operation_id: "turn",
  worker_label: null,
  created_at: "2026-09-05T14:00:00Z",
}));

for (const width of [1040, 390]) {
  test(`the chip counts the chat's artifacts and a row opens the viewer at ${width}px`, async () => {
    const server = await createServer({
      root: new URL("..", import.meta.url).pathname,
      logLevel: "silent",
      server: { host: "127.0.0.1", port: 0 },
    });
    let browser;
    try {
      await server.listen();
      browser = await chromium.launch({ headless: true });
      const page = await browser.newPage({ viewport: { width, height: 760 } });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.route("**/api/projects/project/chats/chat/artifact-list", (route) =>
        route.fulfill({ json: artifacts }),
      );
      await page.goto(
        `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/chatArtifactsPane.html`,
      );
      const chip = page.getByRole("button", { name: "Artifacts", exact: true });
      await chip.waitFor();
      assert.equal((await chip.textContent()).trim(), String(artifacts.length));
      await chip.click();
      const pane = page.getByRole("complementary", { name: "Artifacts" });
      assert.equal(await pane.getByRole("listitem").count(), artifacts.length);
      const box = await pane.boundingBox();
      const composer = await page.locator(".chat-composer").boundingBox();
      if (width > 560) assert.ok(composer.x + composer.width <= box.x + 1, "the chat sits beside");
      else assert.deepEqual([box.x, box.width], [0, width]);
      await pane.getByRole("button", { name: "table.html" }).click();
      assert.deepEqual(await page.evaluate(() => window.opened), ["table"]);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
      assert.deepEqual(errors, []);
    } finally {
      await browser?.close();
      await server.close();
    }
  });
}
