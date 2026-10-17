import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

test("Address starts the flag's chat without opening it", async () => {
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
    const page = await browser.newPage();
    const flag = { flag_id: "flag-1", evidence_id: "e/claim", kind: "overclaim", finding: "" };
    await page.route("**/evidence-flags/flag-1/address", (route) =>
      route.fulfill({
        json: { flag: { ...flag, state: "addressing", chat_id: "chat-1" }, chat_id: "chat-1" },
      }),
    );
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/evidenceFlagAddress.html`);
    await page.getByRole("button", { name: "Address" }).click();
    const openChat = page.getByRole("button", { name: "Open chat" });
    await openChat.waitFor();
    assert.deepEqual(await page.evaluate(() => window.opened), []);
    await openChat.click();
    assert.deepEqual(await page.evaluate(() => window.opened), ["chat-1"]);
  } finally {
    await browser?.close();
    await server.close();
  }
});
