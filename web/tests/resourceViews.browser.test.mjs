import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("artifact and terminal resources render at phone width and refresh through the store", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 8469, strictPort: true },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let entries = [];
    await page.route("**/api/projects/project/artifacts", (route) =>
      route.fulfill({ json: entries }),
    );
    await page.goto("http://127.0.0.1:8469/tests/fixtures/artifacts.html");
    await page.locator('.artifacts-view:not([aria-busy="true"])').waitFor();
    entries = [
      {
        id: "a",
        artifact_id: "a",
        name: "A saved figure",
        available: true,
        can_open: false,
        can_download: false,
        view: "image",
      },
    ];
    await page.evaluate(async () =>
      (await import("/tests/fixtures/resourceRuntime.ts")).store.observe("project", [
        { name: "artifacts", version: "changed" },
      ]),
    );
    await page.getByRole("heading", { name: "A saved figure" }).waitFor();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await page.route("**/api/projects/alpha/terminals**", (route) =>
      route.fulfill({
        json: route.request().url().endsWith("/repositories")
          ? [{ repository_id: "code", path: "/workspace/code", eligible: true }]
          : [],
      }),
    );
    await page.goto("http://127.0.0.1:8469/tests/fixtures/terminals.html");
    await page.getByRole("button", { name: "Terminals", exact: true }).click();
    await page.getByRole("heading", { name: "Terminals" }).waitFor();
    await page.getByRole("button", { name: "code /workspace/code" }).waitFor();
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
