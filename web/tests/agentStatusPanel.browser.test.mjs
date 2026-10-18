import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

test("the agent status panel opens inside the window that holds its strip", async () => {
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
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1000, height: 1100 } });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/agentStatusPanel.html`);
    for (const container of ["#node-window", "#agents-header"]) {
      const host = page.locator(container);
      await host.getByRole("button", { name: "Context" }).click();
      const panel = await host.getByRole("dialog", { name: "Agent status" }).boundingBox();
      const bounds = await host.boundingBox();
      assert.ok(panel.x >= bounds.x, `${container}: panel starts inside its window`);
      assert.ok(
        panel.x + panel.width <= bounds.x + bounds.width,
        `${container}: panel ends inside`,
      );
      await page.keyboard.press("Escape");
    }
  } finally {
    await browser?.close();
    await server.close();
  }
});
