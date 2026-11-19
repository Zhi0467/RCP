import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

let server;
let browser;
let base;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  base = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/linkGuardShell.html`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

test("under the desktop shell's link handler a share link opens in place and others open past it", async () => {
  const page = await browser.newPage();
  await page.goto(base);
  await page.locator("body[data-ready]").waitFor();
  await page.click("#share");
  assert.deepEqual(await page.evaluate(() => window.invoked), []);
  assert.equal(await page.evaluate(() => window.shareRequest()?.token), "shared-token");
  assert.equal(new URL(page.url()).pathname, "/tests/fixtures/linkGuardShell.html");
  await page.click("#outside");
  await page.click("#handled");
  assert.deepEqual(await page.evaluate(() => window.invoked), []);
  assert.deepEqual(await page.evaluate(() => window.opened), ["https://example.com/paper"]);
  assert.equal(await page.getAttribute("#outside", "target"), "_blank");
  await page.click("#here");
  assert.equal(await page.evaluate(() => window.location.hash), "");
});
