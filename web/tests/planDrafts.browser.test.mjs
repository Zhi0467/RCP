import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("plan edits survive stack navigation and are sent by question id", async () => {
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
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/planDrafts.html`,
    );
    await page.getByRole("button", { name: "Edit goal", exact: true }).click();
    await page.getByRole("textbox", { name: "Goal", exact: true }).fill("Edited study goal");
    await page.getByRole("spinbutton", { name: "Turn limit", exact: true }).fill("12");
    await page.getByRole("button", { name: /^Experiment agent:/ }).click();
    await page.locator(".agent-config-fields select").first().selectOption("claude");
    await page.getByRole("button", { name: "Next card", exact: true }).click();
    await page.getByRole("button", { name: "Edit instruction", exact: true }).click();
    await page
      .getByRole("textbox", { name: "Starting instruction", exact: true })
      .fill("Edited exploration");
    await page.getByRole("spinbutton", { name: "Turn limit", exact: true }).fill("9");
    await page.getByRole("button", { name: /^Orchestrator agent:/ }).click();
    await page.locator(".agent-config-fields select").first().selectOption("claude");
    await page.getByRole("button", { name: "Previous card", exact: true }).click();
    await page.getByRole("button", { name: "Start", exact: true }).click();
    await page.waitForFunction(() => window.planDecisions.length === 1);
    await page.getByRole("button", { name: "Next card", exact: true }).click();
    await page.getByRole("button", { name: "Start", exact: true }).click();
    await page.waitForFunction(() => window.planDecisions.length === 2);
    const sent = await page.evaluate(() => window.planDecisions);
    assert.equal(sent[0].id, "q0");
    assert.equal(sent[0].decision.start.goal, "Edited study goal");
    assert.equal(sent[0].decision.start.invocation_ceiling, 12);
    assert.equal(sent[0].decision.start.provider, "claude");
    assert.equal(sent[1].id, "q1");
    assert.equal(sent[1].decision.start.starting_instruction, "Edited exploration");
    assert.equal(sent[1].decision.start.invocation_ceiling, 9);
    assert.equal(sent[1].decision.start.provider, "claude");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
