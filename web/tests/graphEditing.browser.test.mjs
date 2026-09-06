import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("human graph controls create built-in nodes, stage connections, undo, and honor read-only", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    // Use the supported reduced-motion mode so targets do not move during a drag.
    const page = await browser.newPage({ reducedMotion: "reduce" });
    const failures = [];
    page.on("pageerror", (error) => failures.push(error.message));
    await page.route("**/api/projects/fixture/graph-edit-options", (route) =>
      route.fulfill({
        json: {
          relations: [
            {
              name: "supports",
              assessment_required_for: [{ source_type: "evidence", target_type: "hypothesis" }],
            },
            { name: "contradicts", assessment_required_for: [] },
          ],
        },
      }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/graphEditing.html`,
    );
    await page.getByRole("button", { name: "New node", exact: true }).click();
    await page.getByLabel("Type", { exact: true }).selectOption("research_question");
    await page.getByLabel("ID slug").fill("new-question");
    await page.getByLabel("Title", { exact: true }).fill("New question");
    await page.getByLabel("Question", { exact: true }).fill("Does the change work?");
    await page.getByRole("button", { name: "Stage", exact: true }).click();
    const request = async () => JSON.parse(await page.getByLabel("Staged request").textContent());
    assert.equal((await request()).custom_nodes[0].type, "research_question");
    assert.equal((await request()).custom_nodes[0].extension_type, null);
    await page.getByRole("button", { name: "Connections", exact: true }).click();
    await page.getByLabel("From", { exact: true }).selectOption("ev/second");
    await page.getByLabel("To", { exact: true }).selectOption("hyp/first");
    await page.getByLabel("Relation", { exact: true }).selectOption("supports");
    await page.getByLabel("Explanation").fill("Measured comparison");
    await page.getByLabel("Relevance", { exact: true }).selectOption("direct");
    await page.getByLabel("Weight", { exact: true }).selectOption("moderate");
    await page.getByRole("button", { name: "Stage connection" }).click();
    const edge = (await request()).added_edges[0];
    assert.equal(edge.source, "ev/second");
    assert.equal(edge.target, "hyp/first");
    assert.equal(edge.relation, "supports");
    assert.equal(edge.explanation, "Measured comparison");
    assert.deepEqual(edge.assessment, {
      relevance: "direct",
      weight: "moderate",
      qualifications: [],
    });
    await page
      .getByRole("button", { name: "Remove supports connection from ev/second to hyp/first" })
      .click();
    assert.deepEqual((await request()).added_edges, []);
    assert.deepEqual((await request()).removed_edge_ids, []);
    await page.getByRole("button", { name: "Close connections" }).click();
    await page.getByRole("button", { name: "Connect from Second result" }).focus();
    await page.keyboard.press("Enter");
    assert.equal(await page.getByLabel("From", { exact: true }).inputValue(), "ev/second");
    assert.equal(await page.getByLabel("To", { exact: true }).inputValue(), "");
    await page.getByRole("button", { name: "Close connections" }).click();
    const handle = page.getByRole("button", { name: "Connect from Second result" });
    await page.getByRole("button", { name: "Research flow", exact: true }).click();
    // Scroll each endpoint into view, including a target outside the DAG viewport.
    await handle.dragTo(page.locator('[data-node-id="hyp/first"]'));
    await page.waitForFunction(
      () => document.querySelector('select[aria-label="To"]')?.value === "hyp/first",
    );
    assert.equal(await page.getByLabel("From", { exact: true }).inputValue(), "ev/second");
    assert.equal(await page.getByLabel("To", { exact: true }).inputValue(), "hyp/first");
    assert.equal(await page.getByLabel("Relevance", { exact: true }).inputValue(), "");
    await page.getByLabel("Relevance", { exact: true }).selectOption("direct");
    await page.getByLabel("Weight", { exact: true }).selectOption("limited");
    assert.equal(await page.getByRole("button", { name: "Stage connection" }).isDisabled(), false);
    await page.getByRole("button", { name: "Remove source from fixture" }).click();
    assert.equal(await page.getByRole("button", { name: "Stage connection" }).isDisabled(), true);
    await page.getByRole("button", { name: "Toggle read-only" }).click();
    assert.equal(await page.getByRole("button", { name: "Stage connection" }).isDisabled(), true);
    assert.equal(
      await page.getByRole("button", { name: "New node", exact: true }).isDisabled(),
      true,
    );
    assert.deepEqual(failures, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
