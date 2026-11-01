import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("failure task action uses its operation and fits a 390px Inbox", async (t) => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    configLoader: "runner",
    server: { host: "127.0.0.1", port: 0 },
  });
  t.after(() => server.close());
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1100, height: 850 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  await page.goto(
    `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/consolidationInbox.html`,
  );
  const open = page.getByRole("button", { name: "Open task", exact: true });
  await open.waitFor();
  assert.equal(await open.count(), 1);
  assert.equal(await open.isEnabled(), true);
  await open.click();
  assert.deepEqual(await page.evaluate(() => window.openedTasks), ["recorded-operation"]);
  await page.setViewportSize({ width: 390, height: 844 });
  await open.click();
  assert.deepEqual(await page.evaluate(() => window.openedTasks), [
    "recorded-operation",
    "recorded-operation",
  ]);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const bounds = await open.boundingBox();
  assert.ok(bounds.width >= 44 && bounds.height >= 44);
  await page.screenshot({ path: "/private/tmp/rcp-consolidation-inbox-390.png" });
  assert.deepEqual(errors, []);
});
