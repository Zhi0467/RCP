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

test("Research flow fits six type columns to width with vertical scrolling and anchored pinch", async () => {
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
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/dagViewport.html?representative`,
    );
    await page.locator(".dag-node").last().waitFor();
    await page.getByRole("button", { name: "Research flow", exact: true }).click();
    await page.waitForFunction(() => {
      const nodes = [...document.querySelectorAll(".dag-node")];
      const columns = new Map();
      for (const node of nodes) {
        const type = node.dataset.nodeId.split("/")[0];
        const positions = columns.get(type) ?? new Set();
        positions.add(node.style.left);
        columns.set(type, positions);
      }
      return (
        nodes.length === 42 &&
        columns.size === 6 &&
        [...columns.values()].every((positions) => positions.size === 1) &&
        new Set(nodes.map((node) => node.style.left)).size === 6
      );
    });
    const readLayout = async () => {
      await page.waitForFunction(() => {
        const canvas = document.querySelector(".dag-canvas");
        return (
          canvas &&
          Math.abs(
            new DOMMatrix(getComputedStyle(canvas).transform).a -
              Number(canvas.style.transform.slice(6, -1)),
          ) < 0.00001
        );
      });
      return page.locator(".dag-scroll").evaluate((scroller) => {
        const rect = scroller.getBoundingClientRect();
        const canvas = scroller.querySelector(".dag-canvas");
        return {
          viewport: {
            left: rect.left + scroller.clientLeft,
            top: rect.top + scroller.clientTop,
            width: scroller.clientWidth,
            height: scroller.clientHeight,
          },
          zoom: Number(canvas.style.transform.slice(6, -1)),
          nodes: [...canvas.querySelectorAll(".dag-node")].map((node) => {
            const bounds = node.getBoundingClientRect();
            return {
              id: node.dataset.nodeId,
              left: bounds.left,
              right: bounds.right,
              top: bounds.top,
              bottom: bounds.bottom,
            };
          }),
        };
      });
    };
    const columns = [
      ["research_question", 2],
      ["hypothesis", 2],
      ["decision", 15],
      ["blocker", 4],
      ["experiment", 7],
      ["evidence", 12],
    ];
    for (const width of [1280, 1800]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.getByRole("button", { name: "Fit", exact: true }).click();
      const { viewport, nodes } = await readLayout();
      assert.equal(nodes.length, 42);
      let previousRight = -Infinity;
      const firstCards = [];
      for (const [type, count] of columns) {
        const lane = nodes.filter((node) => node.id.startsWith(`${type}/`));
        assert.equal(lane.length, count);
        assert.ok(
          lane.every((node) => Math.abs(node.left - lane[0].left) < 1),
          `${type} shares one column`,
        );
        assert.ok(lane[0].left > previousRight, `${type} follows the preceding type`);
        previousRight = lane[0].right;
        firstCards.push([...lane].sort((left, right) => left.top - right.top)[0]);
      }
      assert.ok(
        firstCards.every((node) => Math.abs(node.top - firstCards[0].top) < 1),
        "All six columns start on the same row",
      );
      assert.ok(
        firstCards.every((node) => node.bottom <= viewport.top + viewport.height),
        "The first card of every type is visible after Fit",
      );
      for (const [index, node] of nodes.entries()) {
        assert.ok(node.left >= viewport.left - 1, `${node.id} is inside the left edge`);
        assert.ok(
          node.right <= viewport.left + viewport.width + 1,
          `${node.id} is inside the right edge`,
        );
        assert.ok(node.top >= viewport.top - 1, `${node.id} is inside the top edge`);
        for (const other of nodes.slice(index + 1)) {
          assert.ok(
            node.right <= other.left ||
              other.right <= node.left ||
              node.bottom <= other.top ||
              other.bottom <= node.top,
            `${node.id} does not overlap ${other.id}`,
          );
        }
      }
      const graphCenterX =
        (Math.min(...nodes.map((node) => node.left)) +
          Math.max(...nodes.map((node) => node.right))) /
        2;
      assert.ok(
        Math.abs(graphCenterX - viewport.left - viewport.width / 2) < 2,
        "Fit centers node bounds horizontally",
      );
      const firstRowTop = Math.min(...nodes.map((node) => node.top));
      assert.ok(firstRowTop > viewport.top, "Fit leaves padding above the first row");
      assert.ok(
        firstRowTop - viewport.top < nodes[0].bottom - nodes[0].top,
        "The first row remains near the top of the viewport",
      );
      const lastDecision = nodes
        .filter((node) => node.id.startsWith("decision/"))
        .sort((left, right) => right.bottom - left.bottom)[0];
      assert.ok(
        lastDecision.bottom > viewport.top + viewport.height,
        "The tall Decision column extends below the initial viewport",
      );
      await page.locator(`[data-node-id="${lastDecision.id}"]`).scrollIntoViewIfNeeded();
      const scrolled = await readLayout();
      const reached = scrolled.nodes.find((node) => node.id === lastDecision.id);
      assert.ok(reached.top >= scrolled.viewport.top, "Scrolling reaches the last Decision");
      assert.ok(reached.bottom <= scrolled.viewport.top + scrolled.viewport.height + 1);
      assert.ok(await page.locator(".dag-scroll").evaluate((scroller) => scroller.scrollTop > 0));
    }

    await page.getByRole("button", { name: "Fit", exact: true }).click();
    const fitted = await readLayout();
    const focalNode = fitted.nodes.find((node) => node.id === "research_question/0");
    const focalX = (focalNode.left + focalNode.right) / 2;
    const focalY = (focalNode.top + focalNode.bottom) / 2;
    await page.locator(".dag-scroll").dispatchEvent("wheel", {
      ctrlKey: true,
      deltaY: -120,
      clientX: focalX,
      clientY: focalY,
    });
    await page.waitForFunction(
      (previousZoom) =>
        Number(document.querySelector(".dag-canvas").style.transform.slice(6, -1)) > previousZoom,
      fitted.zoom,
    );
    const zoomed = await readLayout();
    const zoomedNode = zoomed.nodes.find((node) => node.id === focalNode.id);
    assert.ok(
      Math.abs((zoomedNode.left + zoomedNode.right) / 2 - focalX) < 2,
      "Pinch preserves the focal graph point horizontally",
    );
    assert.ok(
      Math.abs((zoomedNode.top + zoomedNode.bottom) / 2 - focalY) < 2,
      "Pinch preserves the focal graph point vertically",
    );

    await page.getByRole("button", { name: "Toggle DAG", exact: true }).click();
    await page.locator(".dag-canvas").waitFor({ state: "detached" });
    await page.getByRole("button", { name: "Toggle DAG", exact: true }).click();
    await page.locator(".dag-node").last().waitFor();
    const restored = await readLayout();
    assert.equal(restored.zoom, zoomed.zoom);
    for (const node of zoomed.nodes) {
      const restoredNode = restored.nodes.find((candidate) => candidate.id === node.id);
      assert.ok(
        Math.abs(restoredNode.left - node.left) < 2,
        `${node.id} retains its horizontal viewport position`,
      );
      assert.ok(
        Math.abs(restoredNode.top - node.top) < 2,
        `${node.id} retains its vertical viewport position`,
      );
    }

    const card = page.locator('[data-node-id="research_question/0"]');
    const pinPosition = () =>
      card.evaluate((node) => ({ left: node.style.left, top: node.style.top }));
    const unpinned = await pinPosition();
    const cardBounds = await card.boundingBox();
    const dragX = cardBounds.x + cardBounds.width / 2;
    const dragY = cardBounds.y + cardBounds.height / 2;
    await page.mouse.move(dragX, dragY);
    await page.mouse.down();
    await page.mouse.move(dragX + 32, dragY + 24, { steps: 6 });
    await page.mouse.up();
    await page.locator('.dag-node.is-pinned[data-node-id="research_question/0"]').waitFor();
    const pinned = await pinPosition();
    assert.notDeepEqual(pinned, unpinned);
    await page.getByRole("button", { name: "Fit", exact: true }).click();
    assert.deepEqual(await pinPosition(), pinned, "Fit preserves the user's pinned graph position");
    await page.getByRole("button", { name: "Toggle DAG", exact: true }).click();
    await page.locator(".dag-canvas").waitFor({ state: "detached" });
    await page.getByRole("button", { name: "Toggle DAG", exact: true }).click();
    await page.locator('.dag-node.is-pinned[data-node-id="research_question/0"]').waitFor();
    assert.deepEqual(await pinPosition(), pinned, "Remount restores the user's pin");

    // A sparse last column with a bottom-edge pin needs trailing scroll room on both axes.
    await page.evaluate(() => {
      localStorage.setItem(
        "rcp:dag-layout:flow:v2:fixture",
        JSON.stringify({ "evidence/0": { x: 2000, y: 700 } }),
      );
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/dagViewport.html?sparse`,
    );
    await page.locator('.dag-node.is-pinned[data-node-id="evidence/0"]').waitFor();
    await page.waitForFunction(() => {
      const scroller = document.querySelector(".dag-scroll");
      return scroller.scrollLeft > 0 && scroller.scrollTop > 0;
    });
    await page.getByRole("button", { name: "Fit", exact: true }).click();
    const sparse = await readLayout();
    assert.equal(sparse.nodes.length, 1);
    const evidence = sparse.nodes[0];
    assert.ok(
      Math.abs(
        (evidence.left + evidence.right) / 2 - sparse.viewport.left - sparse.viewport.width / 2,
      ) < 2,
      "A sparse last-column pin is centered horizontally instead of hitting the scroll limit",
    );
    assert.ok(
      Math.abs(
        (evidence.top + evidence.bottom) / 2 - sparse.viewport.top - sparse.viewport.height / 2,
      ) < 2,
      "A sparse bottom-edge pin is centered vertically instead of hitting the scroll limit",
    );
    assert.deepEqual(failures, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
