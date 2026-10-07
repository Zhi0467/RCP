import assert from "node:assert/strict";
import test from "node:test";

import { chromium } from "playwright";
import { createServer } from "vite";
import { timelineFixture } from "./fixtures/timeline.mjs";

test("reopening an indexed Experiment restores selection when its exact hash is unchanged", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  try {
    await server.listen();
    const address = server.httpServer?.address();
    assert.ok(address && typeof address === "object");
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const exactHash =
      "#/projects/project-one?view=runs&experiment=experiment%2Fbranch-child&episode=child-experiment-episode&target=branch&branch=auto-research-parent&parent=auto-research-parent";
    // The parent card's Timeline is the only projection that links to the child;
    // the retired Turns list is gone. Serve the actor projection the app would fetch.
    await page.route("**/api/projects/**/timeline", (route) =>
      route.fulfill({
        json: timelineFixture("auto-research-parent", "auto_research", {
          actors: [
            {
              actor_id: "experiment:one",
              kind: "experiment",
              label: "Reproduce the baseline",
              subtitle: null,
              row_key: "experiment:baseline",
              owner_episode_id: "child-experiment-episode",
              started_at: "2026-09-24T10:01:00Z",
              ended_at: null,
              outcome: "running",
              started_by_span_id: null,
              links: {
                episode_id: "child-experiment-episode",
                control_node_id: "experiment/branch-child",
              },
            },
          ],
        }),
      }),
    );
    await page.goto(
      `http://127.0.0.1:${address.port}/tests/fixtures/indexedExperimentReopen.html${exactHash}`,
    );

    await page.locator('[data-selected-episode="child-experiment-episode"]').waitFor();
    await page
      .getByRole("button", { name: "Collapse Experiment loop episode Reproduce the baseline" })
      .click();
    await page
      .getByRole("button", { name: "Expand Experiment loop episode Reproduce the baseline" })
      .waitFor();
    assert.equal(
      await page.locator('[data-selected-episode="child-experiment-episode"]').count(),
      0,
    );

    await page.evaluate(() => {
      window.hashChangesAfterCollapse = 0;
      window.addEventListener("hashchange", () => {
        window.hashChangesAfterCollapse += 1;
      });
    });
    await page
      .getByRole("button", { name: "Expand Experiment loop episode Reproduce the baseline" })
      .click();

    await page.locator('[data-selected-episode="child-experiment-episode"]').waitFor();
    assert.equal(await page.evaluate(() => window.location.hash), exactHash);
    assert.equal(await page.evaluate(() => window.hashChangesAfterCollapse), 0);

    await page
      .getByRole("button", { name: "Collapse Experiment loop episode Reproduce the baseline" })
      .click();
    assert.equal(
      await page.locator('[data-selected-episode="child-experiment-episode"]').count(),
      0,
    );

    const timeline = page.getByRole("region", { name: "Episode timeline" });
    await timeline.getByRole("button", { name: "Reproduce the baseline", exact: true }).click();
    await timeline.getByRole("button", { name: "Open Experiment" }).click();

    await page.locator('[data-selected-episode="child-experiment-episode"]').waitFor();
    assert.equal(await page.evaluate(() => window.location.hash), exactHash);
    assert.equal(await page.evaluate(() => window.hashChangesAfterCollapse), 0);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("same-node loops keep exact selection, detail focus, and independent Stop requests", async () => {
  const serverErrors = [];
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  const pendingStops = [];
  try {
    await server.listen();
    // Listen retries a busy port itself; record only errors after the server is up.
    server.httpServer?.on("error", (error) => serverErrors.push(String(error)));
    const address = server.httpServer.address();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const errors = [];
    const failedRequests = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("requestfailed", (request) => failedRequests.push(request.url()));
    await page.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/stop")) {
        pendingStops.push(route);
        return;
      }
      if (url.pathname.endsWith("/timeline")) {
        return route.fulfill({
          json: timelineFixture(url.pathname.split("/").at(-2), "experiment_loop"),
        });
      }
      return route.fulfill({ json: [] });
    });
    await page.goto(
      `http://127.0.0.1:${address.port}/tests/fixtures/indexedExperimentReopen.html?coexisting`,
    );
    const main = page.locator('[data-episode-id="main-episode"]');
    const branch = page.locator('[data-episode-id="child-experiment-episode"]');
    await branch.locator('[data-selected-episode="child-experiment-episode"]').waitFor();
    assert.equal(await main.count(), 1);
    assert.equal(await branch.count(), 1);
    assert.equal(
      await main.locator('.experiment-branch-badge[data-graph-target-kind="main"]').count(),
      1,
    );
    assert.equal(
      await branch.locator('.experiment-branch-badge[title="auto-research-parent"]').count(),
      1,
    );

    await main.locator(".campaign-run-toggle").click();
    await main.locator('[data-selected-episode="main-episode"]').waitFor();
    assert.equal(await branch.locator("[data-selected-episode]").count(), 0);
    assert.equal(
      await page.evaluate(
        () => document.activeElement.closest("[data-episode-id]")?.dataset.episodeId,
      ),
      "main-episode",
    );
    assert.equal(await branch.locator(".experiment-stop-loop").isEnabled(), true);

    // Reselect the branch, leaving main visible with its own actions.
    await branch.locator(".campaign-run-toggle").click();
    await branch.locator(".campaign-run-toggle").click();
    await branch.locator('[data-selected-episode="child-experiment-episode"]').waitFor();
    assert.equal(await main.locator("[data-selected-episode]").count(), 0);
    assert.equal(
      await page.evaluate(
        () => document.activeElement.closest("[data-episode-id]")?.dataset.episodeId,
      ),
      "child-experiment-episode",
    );

    await branch.locator(".experiment-stop-loop").click();
    await page.waitForFunction(
      () =>
        document.querySelector('[data-episode-id="child-experiment-episode"] .experiment-stop-loop')
          ?.disabled,
    );
    assert.equal(await main.locator(".experiment-stop-loop").isEnabled(), true);
    await main.locator(".experiment-stop-loop").click();
    await page.waitForFunction(
      () =>
        document.querySelector('[data-episode-id="main-episode"] .experiment-stop-loop')?.disabled,
    );
    assert.deepEqual(
      pendingStops.map((route) => new URL(route.request().url()).searchParams.get("episode_id")),
      ["child-experiment-episode", "main-episode"],
    );
    assert.ok(pendingStops.every((route) => route.request().method() === "POST"));
    assert.ok(
      pendingStops.every((route) => !new URL(route.request().url()).searchParams.has("branch_id")),
    );
    await pendingStops.shift().fulfill({ json: {} });
    await page.waitForFunction(
      () =>
        !document.querySelector(
          '[data-episode-id="child-experiment-episode"] .experiment-stop-loop',
        )?.disabled,
    );
    assert.equal(await main.locator(".experiment-stop-loop").isDisabled(), true);
    await pendingStops.shift().fulfill({ json: {} });
    await page.waitForFunction(
      () =>
        !document.querySelector('[data-episode-id="main-episode"] .experiment-stop-loop')?.disabled,
    );
    assert.deepEqual(errors, []);
    assert.deepEqual(failedRequests, []);
    assert.deepEqual(serverErrors, []);
  } finally {
    for (const route of pendingStops) await route.fulfill({ json: {} });
    await browser?.close();
    await server.close();
  }
});
