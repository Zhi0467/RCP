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

    await page.getByText("Selected child transcript").waitFor();
    await page
      .getByRole("button", { name: "Collapse Experiment loop episode Reproduce the baseline" })
      .click();
    await page
      .getByRole("button", { name: "Expand Experiment loop episode Reproduce the baseline" })
      .waitFor();
    assert.equal(await page.getByText("Selected child transcript").count(), 0);

    await page.evaluate(() => {
      window.hashChangesAfterCollapse = 0;
      window.addEventListener("hashchange", () => {
        window.hashChangesAfterCollapse += 1;
      });
    });
    await page
      .getByRole("button", { name: "Expand Experiment loop episode Reproduce the baseline" })
      .click();

    await page.getByText("Selected child transcript").waitFor();
    assert.equal(await page.evaluate(() => window.location.hash), exactHash);
    assert.equal(await page.evaluate(() => window.hashChangesAfterCollapse), 0);

    await page
      .getByRole("button", { name: "Collapse Experiment loop episode Reproduce the baseline" })
      .click();
    assert.equal(await page.getByText("Selected child transcript").count(), 0);

    const timeline = page.getByRole("region", { name: "Episode timeline" });
    await timeline.getByRole("button", { name: "Reproduce the baseline", exact: true }).click();
    await timeline.getByRole("button", { name: "Open Experiment" }).click();

    await page.getByText("Selected child transcript").waitFor();
    assert.equal(await page.evaluate(() => window.location.hash), exactHash);
    assert.equal(await page.evaluate(() => window.hashChangesAfterCollapse), 0);
  } finally {
    await browser?.close();
    await server.close();
  }
});
