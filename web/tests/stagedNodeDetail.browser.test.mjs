import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";
let server, browser, origin;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});
for (const width of [1000, 390]) {
  test(`staged detail edits fields and resolves unknown origin at ${width}px`, async () => {
    const page = await browser.newPage({ viewport: { width, height: 850 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("response", (response) => {
      if (response.status() >= 400) errors.push(response.url());
    });
    try {
      await page.goto(`${origin}/tests/fixtures/stagedNodeDetail.html`);
      await page.getByRole("button", { name: "Edit node", exact: true }).click();
      const done = page.getByRole("button", { name: "Done", exact: true });
      assert.equal(await done.isDisabled(), true);
      await page.getByLabel("Title", { exact: true }).fill("Revised observation");
      await page.getByLabel("Observation", { exact: true }).fill("Measured result");
      await page.getByLabel("Origin", { exact: true }).selectOption("analytic");
      await page.getByLabel("Score *", { exact: true }).fill("");
      assert.equal(await done.isDisabled(), true);
      await page.getByLabel("Score *", { exact: true }).fill("0");
      assert.equal(await done.isEnabled(), true);
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
      );
      await done.click();
      const saved = JSON.parse(await page.getByTestId("saved").textContent());
      assert.equal(saved.title, "Revised observation");
      assert.equal(saved.observation, "Measured result");
      assert.equal(saved.origin, "analytic");
      assert.deepEqual(saved.extension_fields, { score: 0 });
      assert.equal("created_rev" in saved, false);
      await page.getByRole("button", { name: "Remove", exact: true }).click();
      assert.equal(await page.locator(".detail-drawer").count(), 0);
      assert.deepEqual(errors, []);
    } finally {
      await page.close();
    }
  });
}

async function experimentDrawer(width, query = "") {
  const page = await browser.newPage({ viewport: { width, height: 850 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: {} }));
  await page.goto(`${origin}/tests/fixtures/taskCards.html?view=drawer${query}`);
  return { page, errors };
}

for (const width of [1000, 390]) {
  test(`the node's Resume shows it is in flight and holds Start at ${width}px`, async () => {
    const { page, errors } = await experimentDrawer(width);
    try {
      const resume = page.locator(".experiment-continue-button");
      const start = page.locator(".experiment-run-button");
      assert.equal(await resume.isEnabled(), true);
      await resume.click();
      await page.locator('.experiment-continue-button[aria-busy="true"]').waitFor();
      assert.equal(await resume.isDisabled(), true);
      assert.equal(await start.isDisabled(), true);
      assert.deepEqual(await page.evaluate(() => window.resumed), [["episode/exp-1", 5]]);
      await page.evaluate(() => window.finishResume());
      await page.locator('.experiment-continue-button:not([aria-busy="true"])').waitFor();
      assert.equal(await resume.isEnabled(), true);
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
      );
      assert.deepEqual(errors, []);
    } finally {
      await page.close();
    }
  });
}

test("the node's Resume and Start wait while a Stop is unsettled", async () => {
  const { page, errors } = await experimentDrawer(1000, "&stop=1");
  try {
    const start = page.locator(".experiment-run-button");
    assert.equal(await page.locator(".experiment-continue-button").isDisabled(), true);
    assert.equal(await start.isDisabled(), true);
    const reasons = await start.getAttribute("aria-describedby");
    assert.equal(await page.locator(`[id="${reasons}"] li`).count(), 1);
    assert.deepEqual(await page.evaluate(() => window.resumed), []);
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
});

test("the launch picker closes on Escape and on a press outside it", async () => {
  const { page, errors } = await experimentDrawer(1000);
  try {
    const chip = page.locator(".agent-config-chip");
    const panel = page.locator(".agent-config");
    await chip.click();
    await panel.waitFor();
    await page.getByLabel("Starting message").fill("Begin with seed two.");
    assert.equal(await panel.count(), 1);
    await page.keyboard.press("Escape");
    assert.equal(await panel.count(), 0);
    assert.equal(await page.locator(".detail-drawer").count(), 1);
    assert.equal(await chip.evaluate((element) => element === document.activeElement), true);
    await chip.click();
    assert.equal(await page.getByLabel("Starting message").inputValue(), "Begin with seed two.");
    await page.locator(".detail-drawer h2, .detail-drawer h1").first().click();
    assert.equal(await panel.count(), 0);
    assert.deepEqual(errors, []);
  } finally {
    await page.close();
  }
});
