import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

test("Artifacts lists durable entries, refreshes after saving and retries failures", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 626, height: 850 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let entries = [];
    let failure = false;
    await page.route("**/api/projects/project/artifacts", (route) =>
      failure
        ? route.fulfill({ status: 503, json: { detail: "Storage unavailable" } })
        : route.fulfill({ json: entries }),
    );
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/artifacts.html`,
    );
    await page.getByText("No saved artifacts or reports yet.").waitFor();
    entries = [
      {
        id: "report:old-episode",
        name: "Validation report",
        kind: "report",
        created_at: "2026-09-09T12:00:00Z",
        path: null,
        operation_id: null,
        artifact_id: null,
        episode_id: "old-episode",
        can_open: true,
        unavailable_reason: null,
        viewer_url: "/api/projects/project/episodes/old-episode/report/viewer",
      },
      {
        id: "artifact:plot",
        name: "Saved plot",
        kind: "artifact",
        created_at: "2026-09-10T12:00:00Z",
        path: "artifacts/plot.html",
        operation_id: "task",
        artifact_id: "plot",
        episode_id: null,
        can_open: true,
        unavailable_reason: null,
        viewer_url: "/api/projects/project/tasks/task/artifacts/plot/viewer",
      },
    ];
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.getByRole("link", { name: "Open Validation report" }).waitFor();
    await page.getByText("Episode old-episode", { exact: true }).waitFor();
    const layout = await page.evaluate(() => {
      const panel = document.querySelector(".artifacts-view");
      const heading = panel.querySelector("h2");
      const row = panel.querySelector("h3");
      return {
        paddingLeft: parseFloat(getComputedStyle(panel).paddingLeft),
        paddingRight: parseFloat(getComputedStyle(panel).paddingRight),
        headingSize: parseFloat(getComputedStyle(heading).fontSize),
        rowSize: parseFloat(getComputedStyle(row).fontSize),
        overflow: document.documentElement.scrollWidth > window.innerWidth,
      };
    });
    assert.ok(layout.paddingLeft > 0 && layout.paddingRight > 0);
    assert.ok(layout.headingSize > layout.rowSize);
    assert.equal(layout.overflow, false);

    assert.equal(
      await page.getByRole("link", { name: "Open Saved plot" }).getAttribute("href"),
      entries[1].viewer_url,
    );
    assert.equal(
      await page.getByRole("link", { name: "Open Validation report" }).getAttribute("target"),
      "_blank",
    );
    failure = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "Storage unavailable" }).waitFor();
    failure = false;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await page.getByRole("alert").waitFor({ state: "detached" });
    await page.getByRole("link", { name: "Open Saved plot" }).waitFor();
    await page.evaluate(() => {
      window.previewCalls = [];
      window.__TAURI_INTERNALS__ = {
        invoke: async (command, args) => {
          window.previewCalls.push({ command, args });
          return { opened: true };
        },
      };
    });
    await page.getByRole("link", { name: "Open Validation report" }).click();
    await page.getByRole("link", { name: "Open Saved plot" }).click();
    await page.waitForFunction(() => window.previewCalls.length === 2);
    assert.deepEqual(await page.evaluate(() => window.previewCalls), [
      {
        command: "open_episode_report_preview",
        args: { projectId: "project", episodeId: "old-episode" },
      },
      {
        command: "open_artifact_preview",
        args: { projectId: "project", taskId: "task", artifactId: "plot" },
      },
    ]);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
