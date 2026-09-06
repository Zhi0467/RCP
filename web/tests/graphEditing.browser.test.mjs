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
          node_prefixes: {
            research_question: "rq",
            hypothesis: "hyp",
            experiment: "exp",
            evidence: "ev",
            decision: "dec",
            blocker: "blk",
          },
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
    assert.equal("created_rev" in (await request()).custom_nodes[0], false);
    // The fixture intentionally leaves the node awaiting backend completion.
    // Its ID must still be reserved so reopening the form cannot overwrite it.
    await page.getByRole("button", { name: "New node", exact: true }).click();
    await page.getByLabel("ID slug").fill("new-question");
    await page.getByLabel("Title", { exact: true }).fill("Replacement question");
    await page.getByLabel("Question", { exact: true }).fill("Would overwrite the first draft?");
    assert.equal(await page.getByRole("button", { name: "Stage", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "Close new node" }).click();
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
    assert.equal("layer" in edge, false);
    assert.equal(
      await page
        .getByRole("button", { name: "Remove relates_to connection from hyp/first to ev/second" })
        .count(),
      0,
    );
    await page
      .getByRole("button", { name: "Remove contradicts connection from hyp/first to ev/second" })
      .click();
    assert.deepEqual((await request()).removed_edge_ids, ["edge/canonical"]);
    await page
      .getByRole("button", {
        name: "Undo removal of contradicts connection from hyp/first to ev/second",
      })
      .click();
    assert.deepEqual((await request()).removed_edge_ids, []);
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
    await page.getByRole("button", { name: "Stage target removal" }).click();
    assert.equal(
      await page.locator('select[aria-label="To"] option[value="hyp/first"]').count(),
      0,
    );
    assert.equal(await page.getByRole("button", { name: "Stage connection" }).isDisabled(), true);
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

test("a failed graph-edit-options load says what is unavailable instead of promising free entry", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ reducedMotion: "reduce" });
    const failures = [];
    page.on("pageerror", (error) => failures.push(error.message));
    await page.route("**/api/projects/fixture/graph-edit-options", (route) =>
      route.fulfill({ status: 503, json: { detail: "Graph edit options unavailable" } }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/graphEditing.html`,
    );

    const notice = page.getByRole("alert");
    await notice.waitFor();
    // Node ids are derived from the backend prefixes, so there is no free-entry
    // path to offer: the form would fill in and never stage.
    assert.doesNotMatch(await notice.textContent(), /free entry/);
    assert.match(await notice.textContent(), /unavailable until this loads/);
    assert.equal(
      await page.getByRole("button", { name: "New node", exact: true }).isDisabled(),
      true,
    );
    assert.equal(await page.getByRole("button", { name: "Retry" }).isDisabled(), false);
    assert.deepEqual(failures, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("a fitted gesture floor survives DagView unmount and remount after zooming in", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ reducedMotion: "reduce" });
    const failures = [];
    page.on("pageerror", (error) => failures.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") failures.push(message.text());
    });
    page.on("requestfailed", (request) => failures.push(request.url()));
    await page.route("**/api/projects/fixture/graph-edit-options", (route) =>
      route.fulfill({ json: { node_prefixes: {}, relations: [] } }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/dagViewport.html`,
    );
    await page.locator(".dag-node").first().waitFor();
    await page.locator(".dag-node").evaluateAll((nodes) => {
      nodes.forEach((node, index) => {
        node.style.left = `${index * 100_000}px`;
        node.style.top = "0px";
      });
    });
    const readZoom = () =>
      page.locator(".dag-canvas").evaluate((canvas) => Number(canvas.style.transform.slice(6, -1)));
    const waitForZoom = (zoom) =>
      page.waitForFunction(
        (zoom) => document.querySelector(".dag-canvas")?.style.transform === `scale(${zoom})`,
        zoom,
      );
    await page.getByRole("button", { name: "Fit", exact: true }).click();
    await page.waitForFunction(
      () => Number(document.querySelector(".dag-canvas").style.transform.slice(6, -1)) < 0.5,
    );
    const fittedZoom = await readZoom();
    assert.ok(fittedZoom > 0 && fittedZoom < 0.5);
    await page.locator(".dag-scroll").dispatchEvent("wheel", {
      ctrlKey: true,
      deltaY: -2_000,
      clientX: 400,
      clientY: 250,
    });
    await page.waitForFunction(
      () => Number(document.querySelector(".dag-canvas").style.transform.slice(6, -1)) > 0.5,
    );
    const zoomedIn = await readZoom();
    await page.getByRole("button", { name: "Toggle DAG", exact: true }).click();
    await page.locator(".dag-canvas").waitFor({ state: "detached" });
    await page.getByRole("button", { name: "Toggle DAG", exact: true }).click();
    await page.locator(".dag-node").first().waitFor();
    await waitForZoom(zoomedIn);
    // Do not Fit again: only the shared viewport can restore the original floor.
    await page.locator(".dag-scroll").dispatchEvent("wheel", {
      ctrlKey: true,
      deltaY: 100_000,
      clientX: 400,
      clientY: 250,
    });
    await waitForZoom(fittedZoom);
    assert.equal(await readZoom(), fittedZoom);
    assert.deepEqual(failures, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("Fit replaces a sprawling layout's gesture floor when the layout becomes compact", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ reducedMotion: "reduce" });
    const failures = [];
    page.on("pageerror", (error) => failures.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") failures.push(message.text());
    });
    page.on("requestfailed", (request) => failures.push(request.url()));
    await page.route("**/api/projects/fixture/graph-edit-options", (route) =>
      route.fulfill({ json: { node_prefixes: {}, relations: [] } }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/graphEditing.html`,
    );
    await page.locator(".dag-node").first().waitFor();
    // Supply deterministic laid-out boxes to Fit without changing the fixture's graph.
    const positionNodes = (spacing) =>
      page.locator(".dag-node").evaluateAll((nodes, spacing) => {
        nodes.forEach((node, index) => {
          node.style.left = `${index * spacing}px`;
          node.style.top = "0px";
        });
      }, spacing);
    const waitForZoom = (zoom) =>
      page.waitForFunction(
        (zoom) => document.querySelector(".dag-canvas")?.style.transform === `scale(${zoom})`,
        zoom,
      );
    await positionNodes(100_000);
    await page.getByRole("button", { name: "Fit", exact: true }).click();
    await waitForZoom(0.05);
    await positionNodes(300);
    await page.getByRole("button", { name: "Fit", exact: true }).click();
    await page.waitForFunction(
      () => Number(document.querySelector(".dag-canvas").style.transform.slice(6, -1)) > 0.5,
    );
    await page.locator(".dag-scroll").dispatchEvent("wheel", {
      ctrlKey: true,
      deltaY: 100_000,
      clientX: 400,
      clientY: 250,
    });
    await waitForZoom(0.5);
    assert.deepEqual(failures, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
