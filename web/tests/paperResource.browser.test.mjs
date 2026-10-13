import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("Paper keeps typed text on a moved-draft refusal, including at phone width", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    optimizeDeps: { entries: ["tests/fixtures/paperResource.html"] },
    server: { host: "127.0.0.1", port: 8467, strictPort: true },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().includes("409"))
        errors.push(message.text());
    });
    let paper = {
      content: "initial draft",
      draft_revision: 1,
      sync_state: "synced",
      canonical_available: false,
    };
    const saves = [];
    await page.route("**/api/projects/p/paper/sessions", (route) => route.fulfill({ json: [] }));
    await page.route("**/api/projects/p/paper", (route) => {
      if (route.request().method() === "PUT") {
        saves.push(route.request().postDataJSON());
        paper = { ...paper, content: "saved elsewhere", draft_revision: 2 };
        return route.fulfill({
          status: 409,
          json: { detail: { code: "paper_draft_moved", message: "Draft moved" } },
        });
      }
      return route.fulfill({ json: paper });
    });
    await page.goto("http://127.0.0.1:8467/tests/fixtures/paperResource.html");
    const editor = page.getByRole("textbox", { name: "Paper introduction Markdown" });
    await editor.fill("my typed words");
    await page.getByRole("button", { name: "Retry save" }).waitFor();
    assert.equal(await editor.inputValue(), "my typed words");
    assert.equal(saves[0].draft_revision, 1);
    assert.equal(saves[0].content, "my typed words");
    assert.equal(await page.getByRole("button", { name: "Create introduction" }).count(), 0);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
