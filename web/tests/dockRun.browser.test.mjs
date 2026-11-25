import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("a paused Experiment in the dock resumes its unused turns from one compact row", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const continued = [];
    await page.route("**/api/projects/*/episodes/*/continue", (route) => {
      continued.push(route.request().postDataJSON());
      return route.fulfill({ status: 409, json: { detail: "refused" } });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/dockRun.html`,
    );
    const resume = page.locator(".episode-run-card .experiment-continue-button");
    await page.waitForFunction(
      () =>
        document.querySelector(".episode-run-card .experiment-continue-button")?.disabled === false,
    );
    assert.equal(await page.locator(".episode-run-card .campaign-reauthorize input").count(), 0);
    const rows = await page.evaluate(() =>
      [".approval-see", ".approval-later", ".experiment-continue-button"].map(
        (selector) =>
          document.querySelector(`.episode-run-card ${selector}`).getBoundingClientRect().top,
      ),
    );
    assert.equal(new Set(rows.map(Math.round)).size, 1);
    await resume.click();
    await page.locator(".episode-run-card .approval-error").waitFor();
    assert.equal(continued.length, 1);
    assert.equal(continued[0].invocation_ceiling, 3);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("a running run stays out of the dock, and its controls draw as one menu", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const fixture = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/dockRun.html`;
    await page.goto(fixture);
    await page.locator(".episode-run-card").waitFor();
    assert.equal(await page.locator(".episode-run-card").count(), 1);
    assert.equal(await page.locator('a[href="#/running"]').count(), 0);
    assert.equal(await page.locator(".dock-runs").count(), 0);

    await page.goto(`${fixture}?view=menu`);
    await page.getByRole("button", { name: "Run actions" }).click();
    const items = page.getByRole("menuitem");
    assert.equal(await items.count(), 3);
    await items.nth(1).click();
    assert.deepEqual(await page.evaluate(() => window.operated), [["turn-root", "pause"]]);
    await page.getByRole("button", { name: "Run actions" }).click();
    await page.getByRole("menuitem").nth(2).click();
    assert.deepEqual(await page.evaluate(() => window.stopped), ["episode/beta"]);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
