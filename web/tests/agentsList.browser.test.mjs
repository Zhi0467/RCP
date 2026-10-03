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

test("Agents board spins working logos, drags to reorder and archive, and opens a card's chat", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/projects/project/chats/*/worktree**", (route) =>
      route.fulfill({ json: { show_chooser: false, binding: null, integration_options: [] } }),
    );
    await page.route("**/api/projects/*/chats/*/questions", (route) => route.fulfill({ json: [] }));
    let display = { archived: ["Archived chat"], titles: {}, pinned: [] };
    await page.route("**/api/projects/*/chat-display", (route) => route.fulfill({ json: display }));
    const archives = [];
    await page.route("**/api/projects/*/chats/*/archive", (route) => {
      const chatId = decodeURIComponent(route.request().url().split("/chats/")[1].split("/")[0]);
      const { archived } = route.request().postDataJSON();
      archives.push([chatId, archived]);
      display = {
        ...display,
        archived: archived
          ? [...display.archived, chatId]
          : display.archived.filter((id) => id !== chatId),
      };
      return route.fulfill({ json: display });
    });
    const port = server.httpServer.address().port;
    await page.goto(`http://127.0.0.1:${port}/tests/fixtures/agentsList.html?board`);
    const column = (name) => page.locator(`[data-board-column="${name}"]`);
    const card = (title) => page.locator("[data-card-id]").filter({ hasText: title });
    const titles = (name) =>
      column(name)
        .locator("[data-card-id]")
        .evaluateAll((cards) => cards.map((c) => c.dataset.cardId));
    await column("archived").getByText("Archived chat").waitFor();

    // A card's menu shows on hover, so Rename, Pin, and Archive are reachable.
    const menu = card("Claude chat").getByRole("button", { name: "More actions for Claude chat" });
    await card("Claude chat").hover();
    assert.equal(await menu.evaluate((button) => getComputedStyle(button).opacity), "1");
    // Only the working agent's logo carries the spinner ring.
    assert.equal(await column("working").locator(".agent-card-avatar[data-working]").count(), 1);
    assert.equal(await column("done").locator(".agent-card-avatar[data-working]").count(), 0);

    const drag = async (from, to) => {
      const start = await from.boundingBox();
      await page.mouse.move(start.x + 40, start.y + 12);
      await page.mouse.down();
      await page.mouse.move(to.x, to.y, { steps: 8 });
      await page.mouse.up();
    };
    const top = async (locator) => {
      const box = await locator.boundingBox();
      return { x: box.x + 40, y: box.y + 4 };
    };
    // Low on the board, below every card: a column takes drops along its whole height.
    const below = async (name) => {
      const board = await page.locator(".agent-board").boundingBox();
      const box = await column(name).boundingBox();
      return { x: box.x + 40, y: board.y + board.height - 8 };
    };
    // Reordering inside a column is the viewer's own order and survives a reload.
    assert.deepEqual(await titles("done"), ["Claude chat", "Codex chat"]);
    await drag(card("Codex chat"), await top(card("Claude chat")));
    assert.deepEqual(await titles("done"), ["Codex chat", "Claude chat"]);
    // A drop on another state column is refused, since the run decides the state, and
    // a working agent cannot be archived. Neither sends a request before the next drop.
    await drag(card("Claude chat"), await below("working"));
    await drag(card("Working chat"), await below("archived"));
    // Archived is the one column a drop changes: in archives, out restores.
    await drag(card("Claude chat"), await below("archived"));
    await column("archived").getByText("Claude chat").waitFor();
    await drag(card("Archived chat"), await top(column("done").locator("[data-card-id]").first()));
    await column("done").getByText("Archived chat").waitFor();
    assert.deepEqual(archives, [
      ["Claude chat", true],
      ["Archived chat", false],
    ]);
    const placed = await titles("done");
    assert.deepEqual(placed, ["Archived chat", "Codex chat"]);
    await page.reload();
    await column("done").getByText("Codex chat").waitFor();
    assert.deepEqual(await titles("done"), placed);

    // Clicking a card opens its chat with the composer ready.
    await card("Codex chat").getByRole("button", { name: "Open Codex chat" }).click();
    await page.locator(".conversation-surface").waitFor();
    assert.equal(await page.locator(".agents-board-view").count(), 0);
    assert.equal(await page.locator(".conversation-list").isHidden(), true);
    await page.waitForFunction(
      () => document.activeElement?.getAttribute("aria-label") === "Message",
    );
    assert.match(await page.locator(".conversation-header-meta").innerText(), /Codex/);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
