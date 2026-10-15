import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

// Exercise the served canvas with callback recording; the draft owner is tested separately.
test("canvas gestures stage atomic updates, retain pending previews, and guard removal", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({
      reducedMotion: "reduce",
      viewport: { width: 1280, height: 1000 },
    });
    const failures = [];
    page.on("pageerror", (error) => failures.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") failures.push(message.text());
    });
    page.on("requestfailed", (request) => failures.push(request.url()));
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/graphEditing.html`,
    );
    await page.locator(".dag-node").first().waitFor();
    const calls = async () => JSON.parse(await page.getByLabel("Canvas calls").textContent());
    const card = (id) => page.locator(`.dag-node[data-node-id="${id}"]`);
    const select = (id) => card(id).locator(".dag-node-inspect").click();
    const selectEdge = async (id) => {
      // Wait until the link's own hit path has sat under its midpoint for several
      // frames: a preview or layout pass may still be moving it.
      const handle = await page.waitForFunction(
        (edgeId) => {
          const path = document.querySelector(`.dag-edge-hit[data-edge-id="${edgeId}"]`);
          const p = path?.getPointAtLength(path.getTotalLength() / 2);
          const screen = p && new DOMPoint(p.x, p.y).matrixTransform(path.getScreenCTM());
          const key =
            screen && document.elementFromPoint(screen.x, screen.y) === path
              ? `${Math.round(screen.x)},${Math.round(screen.y)}`
              : null;
          const probe = window.__edgeProbe;
          window.__edgeProbe = { key, frames: key && probe?.key === key ? probe.frames + 1 : 0 };
          return window.__edgeProbe.frames >= 10 ? { x: screen.x, y: screen.y } : null;
        },
        id,
        { polling: "raf" },
      );
      const point = await handle.jsonValue();
      await page.mouse.click(point.x, point.y);
    };
    await page.getByRole("button", { name: "Layout", exact: true }).click();
    await page.getByRole("button", { name: "Research flow", exact: true }).click();
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Fit", exact: true }).click();
    await page.getByLabel("Hold preview").check();
    const scroll = await page.locator(".dag-scroll").boundingBox();
    await page.mouse.dblclick(scroll.x + scroll.width - 90, scroll.y + scroll.height - 80);
    await page.getByLabel("Node title", { exact: true }).fill("Canvas result");
    await page.locator('[data-node-type="evidence"]').click();
    await page.getByLabel("Node title", { exact: true }).press("Enter");
    await page.locator(".dag-staged-card").waitFor();
    assert.equal(await card("ev/canvas-result").count(), 0);
    await page.getByLabel("Hold preview").uncheck();
    await card("ev/canvas-result").waitFor();
    assert.equal(await page.locator(".dag-staged-card").count(), 0);
    assert.ok(
      await card("ev/canvas-result").evaluate((node) => node.classList.contains("is-pinned")),
    );
    await select("hyp/accepted");
    assert.equal(
      await page.getByRole("button", { name: "Delete", exact: true }).isDisabled(),
      true,
    );
    const beforeGuard = (await calls()).length;
    await page.keyboard.press("Delete");
    assert.equal((await calls()).length, beforeGuard);
    await select("hyp/first");
    await page.keyboard.press("F2");
    await page.getByLabel("Node title", { exact: true }).fill("Renamed claim");
    await page.getByLabel("Node title", { exact: true }).press("Enter");
    await page.getByRole("button", { name: "Undo", exact: true }).click();
    // Drag to the eligible handle with a small offset, exercising snapping.
    const source = await card("hyp/duplicate").locator(".dag-connect-handle").boundingBox();
    const target = await card("hyp/first").locator(".dag-connect-handle").boundingBox();
    await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2);
    await page.mouse.down();
    await page.mouse.move(target.x + target.width / 2 + 12, target.y + target.height / 2, {
      steps: 8,
    });
    await page.mouse.up();
    const menu = page.locator('[data-relation="relates_to"]').first();
    if (await menu.count()) await menu.click();
    await page.waitForFunction(() =>
      JSON.parse(document.querySelector('output[aria-label="Canvas calls"]').textContent).some(
        (call) => call.method === "onStageEdge",
      ),
    );
    // Select a link and replace its relation in the same chip, then unlink it.
    await selectEdge("edge/canonical");
    await page.locator(".dag-edge-chip button").first().click();
    await page.locator('[data-relation="supports"]').first().click();
    await page.getByRole("button", { name: "Direct", exact: true }).click();
    await page.getByRole("button", { name: "Moderate", exact: true }).click();
    const linkButton = page
      .locator(".dag-edit-menu")
      .getByRole("button", { name: "Link", exact: true });
    await page.getByRole("button", { name: "Matched", exact: true }).click();
    await page.getByLabel("Explanation", { exact: true }).fill("");
    assert.equal(await linkButton.isDisabled(), true);
    await page.getByLabel("Explanation", { exact: true }).fill("Same direction as planned");
    await page.getByLabel("Qualifications", { exact: true }).fill("One run\n\nOne run\nSmall n");
    await linkButton.click();
    await page.waitForFunction(() =>
      JSON.parse(document.querySelector('output[aria-label="Canvas calls"]').textContent).some(
        (call) => call.method === "onReplaceEdge",
      ),
    );
    const replaced = (await calls()).find((call) => call.method === "onReplaceEdge");
    assert.equal(replaced.args[1].expectation, "matched");
    assert.ok(replaced.args[1].explanation.length > 0);
    assert.deepEqual(replaced.args[1].assessment.qualifications, ["One run", "Small n"]);
    // The current relation reopens the same details to edit them in place.
    await selectEdge(replaced.args[1].id);
    await page.locator(".dag-edge-chip button").first().click();
    await page.locator('[data-relation="supports"]').first().click();
    await page.getByLabel("Explanation", { exact: true }).fill("Edited reason");
    await linkButton.click();
    await page.waitForFunction(
      () =>
        JSON.parse(document.querySelector('output[aria-label="Canvas calls"]').textContent).filter(
          (call) => call.method === "onReplaceEdge",
        ).length === 2,
    );
    const edited = (await calls()).filter((call) => call.method === "onReplaceEdge")[1];
    assert.equal(edited.args[0], replaced.args[1].id);
    assert.equal(edited.args[1].expectation, "matched");
    // Undo removes the selected draft link, and its chip with it.
    await selectEdge(edited.args[1].id);
    await page.locator(".dag-edge-chip").waitFor();
    await page.getByRole("button", { name: "Undo", exact: true }).click();
    await page.locator(".dag-edge-chip").waitFor({ state: "detached" });
    await selectEdge(replaced.args[1].id);
    await page.getByRole("button", { name: "Unlink", exact: true }).click();
    // Hold the duplicate over the keeper until the merge ring arms.
    const duplicate = await card("hyp/duplicate").boundingBox();
    const keeper = await card("hyp/first").boundingBox();
    await page.mouse.move(duplicate.x + duplicate.width / 2, duplicate.y + duplicate.height / 2);
    await page.mouse.down();
    await page.mouse.move(keeper.x + keeper.width / 2, keeper.y + keeper.height / 2, { steps: 8 });
    await page.locator('.dag-node.is-merge-armed[data-node-id="hyp/first"]').waitFor();
    await page.mouse.up();
    await page.getByRole("button", { name: "Confirm merge", exact: true }).click();
    const recorded = await calls();
    assert.ok(
      recorded.some(
        (call) =>
          call.method === "onStageNode" &&
          call.args[0].id === "ev/canvas-result" &&
          call.args[0].origin === "unknown",
      ),
    );
    assert.ok(
      recorded.some(
        (call) =>
          call.method === "onRenameNode" &&
          call.args[0] === "hyp/first" &&
          call.args[1] === "Renamed claim",
      ),
    );
    assert.ok(recorded.some((call) => call.method === "onUndo"));
    assert.ok(recorded.some((call) => call.method === "onRemoveEdge"));
    assert.ok(
      recorded.some(
        (call) =>
          call.method === "onMergeNodes" &&
          call.args[0] === "hyp/duplicate" &&
          call.args[1] === "hyp/first",
      ),
    );
    await page.getByRole("button", { name: "Toggle read-only", exact: true }).click();
    assert.equal(await page.locator(".dag-connect-handle").count(), 0);
    assert.equal(await page.getByRole("button", { name: "Delete", exact: true }).count(), 0);
    assert.deepEqual(failures, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("phone canvas keeps creation and pick controls inside the viewport", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
      reducedMotion: "reduce",
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/graphEditing.html`,
    );
    await page.locator(".dag-node").first().waitFor();
    await page.getByRole("button", { name: "Node", exact: true }).tap();
    await page.getByLabel("Node title", { exact: true }).fill("Phone node");
    const staged = await page.locator(".dag-staged-card").boundingBox();
    assert.ok(staged.width >= 230);
    await page.getByLabel("Node title", { exact: true }).press("Enter");
    await page.locator('[data-node-id="rq/phone-node"]').waitFor();
    const source = page.locator('[data-node-id="hyp/duplicate"]');
    await source.locator(".dag-node-inspect").tap();
    const handle = await source.locator(".dag-connect-handle").boundingBox();
    assert.ok(handle.width >= 29 && handle.height >= 29);
    await source.locator(".dag-connect-handle").tap();
    await page.locator(".dag-pick-chip").waitFor();
    await page.locator('[data-node-id="hyp/first"] .dag-node-inspect').tap();
    const menu = page.locator(".dag-edit-menu");
    await menu.waitFor();
    const bounds = await menu.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
    await menu.locator('[data-relation="relates_to"]').first().tap();
    assert.equal(await page.locator(".dag-edit-menu").count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > 390), false);
    assert.deepEqual(errors, []);
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
    await page.getByRole("button", { name: "Layout", exact: true }).click();
    await page.getByRole("button", { name: "Research flow", exact: true }).click();
    await page.keyboard.press("Escape");
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
