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
          : requests.length === 3
            ? { status: 422, json: { detail: { code: "branch_fork_invalid", message: "refused" } } }
            : { json: { kind: "branch", branch_id: requests.at(-1).request_id } },
      );
    });
    const url = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/historyFork.html`;
    await page.goto(url);
    await page
      .locator('[data-revision="7"]')
      .getByRole("button", { name: "Fork from here", exact: true })
      .click();
    await page.getByRole("button", { name: "Retry fork" }).waitFor();
    await Promise.all([
      page.waitForResponse((response) => response.url().endsWith("/graph-refs")),
      page.getByRole("button", { name: "Retry fork" }).click(),
    ]);
    await page
      .locator('[data-revision="7"]')
      .getByRole("button", { name: "Fork from here", exact: true })
      .waitFor();
    assert.deepEqual(requests[0], requests[1]);
    assert.equal(requests[0].at, "main@7");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${url}?branch=1`);
    assert.equal(
      await page
        .locator('[data-revision="7"]')
        .getByRole("button", { name: "Fork from here", exact: true })
        .isVisible(),
      false,
    );
    const menuButton = page.getByRole("button", { name: "Revision actions" });
    await menuButton.click();
    await page.getByRole("heading", { name: "Project history", exact: true }).click();
    assert.equal(await page.getByRole("menu").count(), 0);
    await menuButton.click();
    await page.keyboard.press("Escape");
    assert.equal(await page.getByRole("menu").count(), 0);
    await menuButton.click();
    await Promise.all([
      page.waitForResponse((response) => response.url().endsWith("/graph-refs")),
      page.getByRole("menuitem", { name: "Fork from here" }).click(),
    ]);
    assert.equal(requests[2].at, "branch:source@7");
    await page.getByRole("alert").filter({ hasText: /.+/ }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Retry fork" }).count(), 0);
    await menuButton.click();
    await Promise.all([
      page.waitForResponse((response) => response.url().endsWith("/graph-refs")),
      page.getByRole("menuitem", { name: "Fork from here" }).click(),
    ]);
    assert.notEqual(requests[2].request_id, requests[3].request_id);
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
