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

test("Address all uses one capped request, retries the same batch, and fits at 390px", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  try {
    await server.listen();
    const address = server.httpServer?.address();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().includes("Failed to load resource"))
        errors.push(message.text());
    });
    const requests = [];
    let singleRequests = 0;
    await page.route("**/evidence-flags/*/address", (route) => {
      singleRequests++;
      return route.abort();
    });
    await page.route("**/evidence-flags/address", (route) => {
      const body = route.request().postDataJSON();
      requests.push(body);
      if (requests.length === 1) return route.abort("failed");
      return route.fulfill({
        json: {
          flags: body.flag_ids.map((id) => ({
            flag_id: id,
            evidence_id: "e/claim",
            kind: "overclaim",
            state: "addressing",
            chat_id: "shared-chat",
          })),
          chat_id: "shared-chat",
          operation_id: "one-turn",
        },
      });
    });
    await page.goto(
      `http://127.0.0.1:${address.port}/tests/fixtures/evidenceFlagAddress.html?count=12`,
    );
    const all = page.getByRole("button", { name: "Address all", exact: true });
    await all.waitFor();
    const bounds = await all.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
    await all.click();
    const retry = page.getByRole("button", { name: "Retry Address all" });
    await retry.waitFor();
    assert.ok(await retry.evaluate((button) => button.classList.contains("has-error")));
    assert.equal(await retry.textContent(), "");
    await retry.click();
    await page.waitForFunction(
      () => document.querySelectorAll(".evidence-flag-row.addressing").length === 10,
    );
    assert.equal(requests.length, 2);
    assert.equal(singleRequests, 0);
    assert.deepEqual(requests[1], requests[0]);
    assert.deepEqual(
      requests[0].flag_ids,
      Array.from({ length: 10 }, (_, i) => `flag-${12 - i}`),
    );
    assert.ok(requests[0].request_id);
    assert.deepEqual(await page.evaluate(() => window.opened), []);
    const chats = page.getByRole("button", { name: "Open chat", exact: true });
    assert.equal(await chats.count(), 10);
    await chats.first().click();
    await chats.last().click();
    assert.deepEqual(await page.evaluate(() => window.opened), ["shared-chat", "shared-chat"]);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= 390));
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
