import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";

test("a failed send stays in place and Resend reuses its request id", async () => {
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
    const page = await browser.newPage({ viewport: { width: 1000, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/chatSend.html`);
    const composer = page.getByRole("textbox", { name: "Message", exact: true });
    const lines = page.locator(".node-chat-lines");
    await composer.waitFor();

    // A reader who moves up even slightly from the bottom keeps their position
    // across a refresh; nothing pulls them back down.
    await lines.evaluate((element) => (element.scrollTop = element.scrollHeight));
    await page.waitForTimeout(100);
    const bottom = await lines.evaluate((element) => element.scrollTop);
    await lines.evaluate((element) => (element.scrollTop -= 30));
    await page.waitForTimeout(100);
    await page.evaluate(() => window.refreshChatFixture());
    await page.waitForTimeout(100);
    assert.equal(await lines.evaluate((element) => element.scrollTop), bottom - 30);

    await composer.fill("Lost on the way");
    await page.getByRole("button", { name: "Start Discuss turn", exact: true }).click();
    const failed = lines.getByRole("button", { name: "Resend", exact: true });
    await failed.waitFor();
    // The message stays where it was sent, and the composer keeps a copy.
    assert.equal(await lines.getByText("Lost on the way", { exact: true }).count(), 1);
    assert.equal(await composer.inputValue(), "Lost on the way");

    await failed.click();
    await page.getByText("Starting", { exact: true }).first().waitFor();
    assert.equal(await failed.count(), 0);
    assert.equal(await lines.getByText("Lost on the way", { exact: true }).count(), 1);
    assert.equal(await composer.inputValue(), "");
    const calls = await page.evaluate(() => window.startCalls);
    assert.equal(calls.length, 2);
    assert.match(calls[0].requestId, /^[0-9a-f-]{36}$/);
    assert.equal(calls[1].requestId, calls[0].requestId);
    // Starting before a provider process exists shows no steering refusal.
    assert.equal(await page.getByText("No live provider process", { exact: false }).count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
