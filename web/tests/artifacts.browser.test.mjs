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
        episode_mode: "experiment_loop",
        source_chat_href:
          "#/projects/project?view=runs&experiment=experiment%2Ftransfer&episode=old-episode&target=branch&branch=branch-one&parent=parent-episode",
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
        episode_mode: "experiment_loop",
        source_chat_href: "#/projects/project?view=chats&branch_id=branch-one&chat=plot-chat",
        can_open: true,
        unavailable_reason: null,
        viewer_url: "/api/projects/project/tasks/task/artifacts/plot/viewer",
      },
      {
        id: "artifact:unavailable",
        name: "Unavailable plot",
        kind: "artifact",
        created_at: "2026-09-10T12:00:00Z",
        path: "artifacts/unavailable.html",
        operation_id: "task",
        artifact_id: "unavailable",
        episode_id: null,
        episode_mode: null,
        source_chat_href: null,
        can_open: false,
        unavailable_reason: "Preview unavailable.",
        viewer_url: null,
      },
      {
        id: "report:no-chat",
        name: "Report",
        kind: "report",
        created_at: "2026-09-10T12:00:00Z",
        path: null,
        operation_id: null,
        artifact_id: null,
        episode_id: "no-chat",
        episode_mode: "auto_research",
        source_chat_href: null,
        can_open: true,
        unavailable_reason: null,
        viewer_url: "/api/projects/project/episodes/no-chat/report/viewer",
      },
    ];
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.getByRole("link", { name: "Open Validation report" }).waitFor();
    await page.getByRole("link", { name: "Open originating chat for Validation report" }).waitFor();
    assert.doesNotMatch(
      await page.locator(".artifacts-list").innerText(),
      /old-episode|artifacts\/plot\.html/,
    );
    assert.equal(await page.locator(".artifacts-list time").count(), 0);
    assert.equal(
      await page
        .getByRole("link", { name: "Open originating chat for Report", exact: true })
        .count(),
      0,
    );
    await page.getByRole("link", { name: "Open Report", exact: true }).waitFor();
    assert.deepEqual(await page.locator(".artifact-episode-tag").allTextContents(), [
      "Experiment",
      "Experiment",
      "Auto-research",
    ]);
    assert.equal(
      await page
        .locator(".artifact-entry")
        .filter({ hasText: "Unavailable plot" })
        .locator(".artifact-episode-tag")
        .count(),
      0,
    );
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
    // The title and whitespace belong to the existing link, in both Aqua modes.
    const report = page.locator(".artifact-entry").filter({ hasText: "Validation report" });
    const unavailable = page.locator(".artifact-entry").filter({ hasText: "Unavailable plot" });
    const classicShadow = await report.evaluate((row) => getComputedStyle(row).boxShadow);
    for (const mode of ["light", "dark"]) {
      await page.evaluate((mode) => {
        document.documentElement.dataset.theme = "aqua";
        document.documentElement.dataset.colorMode = mode;
      }, mode);
      const raised = await report.evaluate((row) => getComputedStyle(row).boxShadow);
      assert.notEqual(raised, "none");
      assert.equal(await unavailable.evaluate((row) => getComputedStyle(row).boxShadow), "none");
      assert.equal(await unavailable.getByRole("link").count(), 0);
      await report.hover({ position: { x: 25, y: 25 } });
      await page.mouse.down();
      const pressed = await report.evaluate((row) => getComputedStyle(row).boxShadow);
      assert.match(pressed, /inset/);
      assert.notEqual(pressed, raised);
      // Moving out cancels navigation while still exercising the pressed state.
      await page.mouse.move(0, 0);
      await page.mouse.up();
      await page.setViewportSize({ width: 360, height: 780 });
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
        false,
      );
      await page.setViewportSize({ width: 626, height: 850 });
    }
    await page.evaluate(() => {
      document.documentElement.dataset.theme = "classic";
      document.documentElement.dataset.colorMode = "light";
    });
    assert.equal(await report.evaluate((row) => getComputedStyle(row).boxShadow), classicShadow);
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
    await report.click({ position: { x: 12, y: 12 } });
    // Each link's handler records its call after its own await, so clicking both
    // before either lands leaves the recorded order to chance.
    await page.waitForFunction(() => window.previewCalls.length === 1);
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
    // The source link stays above the stretched preview hit target and follows
    // the same app window, even in the native shell.
    for (const theme of ["classic", "aqua"]) {
      await page.evaluate((theme) => {
        document.documentElement.dataset.theme = theme;
      }, theme);
      for (const entry of entries.slice(0, 2)) {
        const source = page.getByRole("link", {
          name: `Open originating chat for ${entry.name}`,
          exact: true,
        });
        await source.click();
        assert.equal(new URL(page.url()).hash, entry.source_chat_href);
        assert.equal(
          await page.evaluate(() => window.previewCalls.length),
          2,
          "Source chat never opens an artifact preview",
        );
      }
      const source = page.getByRole("link", {
        name: "Open originating chat for Saved plot",
        exact: true,
      });
      await source.focus();
      await page.keyboard.press("Enter");
      assert.equal(new URL(page.url()).hash, entries[1].source_chat_href);
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
