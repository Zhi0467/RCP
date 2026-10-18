import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("history forks exact main and branch revisions with phone overflow and idempotent retry", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1200, height: 850 } });
    const errors = [];
    const requests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/projects/project/graph-refs", async (route) => {
      requests.push(route.request().postDataJSON());
      await route.fulfill(
        requests.length === 1
          ? { status: 503, json: { detail: "temporary" } }
          : { json: { kind: "branch", branch_id: requests.at(-1).request_id } },
      );
    });
    const url = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/historyFork.html`;
    await page.goto(url);
    await page.locator(".history-fork-action").click();
    await page.locator(".history-fork-failed").waitFor();
    await Promise.all([
      page.waitForResponse((response) => response.url().endsWith("/graph-refs")),
      page.getByRole("button", { name: "Retry fork" }).click(),
    ]);
    await page.locator(".history-fork-action").waitFor();
    assert.deepEqual(requests[0], requests[1]);
    assert.equal(requests[0].at, "main@7");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${url}?branch=1`);
    assert.equal(await page.locator(".history-fork-action").isVisible(), false);
    await page.getByLabel("Revision actions").click();
    await Promise.all([
      page.waitForResponse((response) => response.url().endsWith("/graph-refs")),
      page.getByRole("button", { name: "Fork from here" }).click(),
    ]);
    assert.equal(requests[2].at, "branch:source@7");
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
