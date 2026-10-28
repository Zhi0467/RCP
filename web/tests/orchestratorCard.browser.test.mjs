import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

async function withCard(chain, check) {
  const ended = chain === "ended";
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1100, height: 800 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const unexpected = [];
    await page.route("**/api/**", (route) => {
      const url = route.request().url();
      if (url.endsWith("/questions")) return route.fulfill({ json: [] });
      if (url.endsWith("/chat-display"))
        return route.fulfill({ json: { archived: [], titles: {}, pinned: [] } });
      // Listed from main, the card reads its own branch's watchers.
      if (/\/watchers\?branch_id=root-episode$/.test(url)) return route.fulfill({ json: [] });
      // Only project routes matter: turns start through onStartTask, never directly,
      // and only an ended card's ordinary composer reads its chat's worktree, on its own
      // branch, and browser.
      const composerRead =
        ended &&
        (/\/chats\/root-episode\/worktree\?.*branch_id=root-episode/.test(url) ||
          /\/chats\/root-episode\/browser\b/.test(url));
      if (url.includes("/api/projects/") && !composerRead) unexpected.push(url);
      return route.fulfill({ status: 404, json: { detail: "unexpected" } });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/orchestratorCard.html?chain=${chain}`,
    );
    const lines = page.locator(".node-chat-lines");
    await lines.getByText("Root findings.").waitFor();
    await check(page, lines);
    assert.deepEqual(unexpected, []);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
}

test("A running orchestrator card shows its turns and its composer messages the orchestrator", async () => {
  await withCard("running", async (page, lines) => {
    // Each turn's recorded human input precedes its answer.
    const texts = await lines.locator(".node-chat-line").allInnerTexts();
    const order = [
      "Trace the evidence.",
      "Root findings.",
      "Check the control.",
      "Control checked.",
    ];
    assert.deepEqual(
      order.map((text) => texts.findIndex((line) => line.includes(text))),
      [0, 1, 2, 3],
    );
    // An open question puts the card in the stopped group, and no chat composer is offered.
    assert.equal(await page.locator('[data-group="stopped"]').getByRole("option").count(), 1);
    assert.equal(await page.getByRole("textbox", { name: "Message", exact: true }).count(), 0);

    await page.getByRole("textbox", { name: "Message orchestrator" }).fill("Try the ablation.");
    await page.getByRole("button", { name: "Send" }).click();
    await page.waitForFunction(() => window.sentMessages.length === 1);
    const [[episodeId, body, turns, requestId]] = await page.evaluate(() => window.sentMessages);
    assert.deepEqual([episodeId, body, turns], ["member-two", "Try the ablation.", undefined]);
    assert.match(requestId, /^[0-9a-f-]{36}$/);
    assert.deepEqual(await page.evaluate(() => window.startedTasks), []);
  });
});

test("An ended orchestrator card takes ordinary chat turns on the chain's branch", async () => {
  await withCard("ended", async (page) => {
    assert.equal(await page.getByRole("textbox", { name: "Message orchestrator" }).count(), 0);
    await page.getByRole("textbox", { name: "Message", exact: true }).fill("Summarize the chain.");
    await page.getByRole("button", { name: "Start Discuss turn", exact: true }).click();
    await page.waitForFunction(() => window.startedTasks.length === 1);
    const [[kind, request, , target]] = await page.evaluate(() => window.startedTasks);
    assert.equal(kind, "project_chat");
    assert.equal(request.chat_id, "root-episode");
    assert.equal(request.message, "Summarize the chain.");
    // Viewed from main, the turn still posts to the card's own branch.
    assert.deepEqual(target, { kind: "branch", branch_id: "root-episode" });
    assert.deepEqual(await page.evaluate(() => window.sentMessages), []);
  });
});
