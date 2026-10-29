import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("the Auto-research code worktree picks among writable repositories and never turns off silently", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const base = `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/autoResearchWorktree.html`;
    const openOptions = async (writable) => {
      await page.goto(`${base}?writable=${writable}`);
      await page.getByRole("button", { name: /Orchestrator agent/ }).click();
      return page.getByRole("checkbox", { name: "Code worktree" });
    };

    // Two writable repositories: the toggle stays on and asks which one.
    const toggle = await openOptions("repo-a,repo-b");
    assert.equal(await toggle.isChecked(), true);
    const picker = page.getByRole("combobox", { name: "Worktree repository" });
    await picker.selectOption("repo-b");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= 390));
    await page.locator('button[type="submit"]').click();
    const [args] = await page.evaluate(() => window.authorized);
    assert.equal(args[2], true);
    assert.equal(args.at(-1), "repo-b");

    // One writable repository: no choice to make, and the worktree names it.
    await openOptions("repo-a");
    assert.equal(await picker.count(), 0);
    await page.locator('button[type="submit"]').click();
    assert.equal((await page.evaluate(() => window.authorized))[0].at(-1), "repo-a");

    // None writable: the toggle is off, disabled, and says why.
    const unavailable = await openOptions("");
    assert.equal(await unavailable.isChecked(), false);
    assert.equal(await unavailable.isDisabled(), true);
    const label = page.locator(".code-worktree-choice label");
    assert.ok((await label.getAttribute("title"))?.length);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
