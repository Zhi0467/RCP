import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("Agents keeps archived chats hidden across revisits and shows provider logos", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/projects/project/chats/*/worktree**", (route) =>
      route.fulfill({ json: { show_chooser: false, binding: null, integration_options: [] } }),
    );
    await page.route("**/api/projects/*/chats/*/questions", (route) => route.fulfill({ json: [] }));
    await page.route("**/api/projects/*/chat-display", (route) =>
      route.fulfill({ json: { archived: ["Archived chat"], titles: {}, pinned: [] } }),
    );
    const port = server.httpServer.address().port;
    await page.goto(`http://127.0.0.1:${port}/tests/fixtures/agentsList.html`);
    const list = page.locator(".conversation-list");
    const row = (title) => list.getByRole("option").filter({ hasText: title });
    await page.getByRole("button", { name: /^Archived/ }).waitFor();
    assert.equal(await row("Archived chat").count(), 0);

    // A known provider renders its logo at text size; an unknown one keeps its label.
    const logo = row("Claude chat").getByRole("img", { name: "Claude", exact: true });
    const box = await logo.boundingBox();
    assert.ok(box.width >= 10 && box.width <= 20 && box.height === box.width);
    assert.equal(await row("Codex chat").getByRole("img", { name: "Codex" }).count(), 1);
    // The logo leads the same metadata the chat header shows: model, effort, task type.
    assert.match(await row("Claude chat").locator(".agent-row-meta").innerText(), /Project chat/);
    await page.getByRole("button", { name: /^Archived/ }).click();
    assert.match(await row("Archived chat").locator(".agent-row-meta").innerText(), /Custom agent/);
    await page.getByRole("button", { name: /^All\s*\d/ }).click();

    // A revisit uses the last display set while its reload is still in flight.
    let releaseDisplay;
    await page.route("**/api/projects/*/chat-display", async (route) => {
      await new Promise((resolve) => (releaseDisplay = resolve));
      await route.fulfill({ json: { archived: ["Archived chat"], titles: {}, pinned: [] } });
    });
    await page.evaluate(async () => {
      const { remountAgents } = await import("/tests/fixtures/agentsList.tsx");
      remountAgents();
    });
    await row("Claude chat").waitFor();
    assert.equal(await row("Archived chat").count(), 0);
    releaseDisplay?.();
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
