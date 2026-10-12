import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("The orchestrator card shows its turns and its composer messages the orchestrator", async () => {
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
      // Only project routes matter: the card must not start a chat turn or load a worktree.
      if (url.includes("/api/projects/")) unexpected.push(url);
      return route.fulfill({ status: 404, json: { detail: "unexpected" } });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/orchestratorCard.html`,
    );
    const lines = page.locator(".node-chat-lines");
    await lines.getByText("Root findings.").waitFor();
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

    await page.getByRole("spinbutton", { name: "Message continuation turns" }).fill("2");
    await page.getByRole("textbox", { name: "Message orchestrator" }).fill("Try the ablation.");
    await page.getByRole("button", { name: "Send" }).click();
    await page.waitForFunction(() => window.sentMessages.length === 1);
    const [[episodeId, body, turns, requestId]] = await page.evaluate(() => window.sentMessages);
    assert.deepEqual([episodeId, body, turns], ["member-two", "Try the ablation.", 2]);
    assert.match(requestId, /^[0-9a-f-]{36}$/);
    assert.deepEqual(unexpected, []);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
