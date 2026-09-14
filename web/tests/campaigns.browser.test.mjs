import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "vite";
import { chromium } from "playwright";
import { rootTask, episode, withGraphBranch } from "./fixtures/campaigns.mjs";

test("a stopped ineligible branch submits a deliberate merge and shows the server refusal beside it", async () => {
  const liveServer = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await liveServer.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("requestfailed", (request) => errors.push(request.failure()?.errorText));
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().includes("409 (Conflict)")) {
        errors.push(message.text());
      }
    });
    const stopped = {
      ...withGraphBranch({ merge_eligible: false }),
      status: "stopped",
      ending: "stopped",
      health: "stopped",
      recommendation: "none",
      can_stop: false,
      can_message: false,
      task_control: null,
      tasks: [],
    };
    const reason = "Branch writers must settle before merging: auto_research paused-turn (paused).";
    let polledEpisode = stopped;
    await page.route("**/api/projects/**/timeline", (route) =>
      route.fulfill({
        json: { episode_id: episode.episode_id, mode: episode.mode, events: [], truncated: false },
      }),
    );
    await page.route("**/fixture/episode", (route) => route.fulfill({ json: polledEpisode }));
    let requests = 0;
    await page.route("**/api/projects/**/merge", async (route) => {
      assert.equal(route.request().method(), "POST");
      requests += 1;
      await route.fulfill(
        requests === 1
          ? { status: 409, json: { detail: reason } }
          : {
              status: 202,
              json: {
                ...stopped,
                graph_branch: {
                  ...stopped.graph_branch,
                  merge_state: "running",
                  active_merge_task_id: "merge-task",
                },
              },
            },
      );
    });
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/branchMerge.html`,
    );
    const merge = page.getByRole("button", { name: "Merge to main", exact: true });
    await merge.click();
    const branch = page.getByRole("region", { name: "Episode graph branch" });
    await branch.getByRole("alert").waitFor();
    assert.equal(await branch.getByRole("alert").textContent(), reason);
    assert.equal(requests, 1);
    assert.equal(await merge.isEnabled(), true);
    // Polling identical state retains the refusal, but a newly eligible snapshot retires it.
    await page.evaluate(() => window.refreshMergeEpisode());
    assert.equal(await branch.getByRole("alert").textContent(), reason);
    polledEpisode = {
      ...stopped,
      graph_branch: { ...stopped.graph_branch, merge_eligible: true },
    };
    await page.evaluate(() => window.refreshMergeEpisode());
    await branch.getByRole("alert").waitFor({ state: "detached" });
    assert.equal(requests, 1);
    // Returning to the earlier snapshot must not resurrect an obsolete refusal.
    polledEpisode = stopped;
    await page.evaluate(() => window.refreshMergeEpisode());
    assert.equal(await branch.getByRole("alert").count(), 0);
    // Eligibility can change after the last snapshot; the second click must reach the server.
    await merge.click();
    await branch.getByText("Merge running", { exact: true }).waitFor();
    assert.equal(await branch.getByRole("alert").count(), 0);
    assert.equal(requests, 2);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
  }
});

test("a served exhausted card settles from wrapping up to a visible report failure", async () => {
  const liveServer = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await liveServer.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("requestfailed", (request) => errors.push(request.failure()?.errorText));
    page.on("response", (response) => {
      if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
    });
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    let projected = {
      ...episode,
      status: "wrapping_up",
      ending: "exhausted",
      wrapup_state: "not_started",
      health: "wrapping_up",
      recommendation: "wait",
      blocked_reason: null,
      task_control: null,
      can_stop: false,
      can_message: false,
      tasks: [{ ...rootTask, status: "succeeded", can_pause: false }],
    };
    let polls = 0;
    await page.route("**/api/projects/**/timeline", (route) =>
      route.fulfill({
        json: { episode_id: episode.episode_id, mode: episode.mode, events: [], truncated: false },
      }),
    );
    await page.route("**/fixture/episode", (route) => {
      polls += 1;
      return route.fulfill({ json: projected });
    });
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/branchMerge.html`,
    );
    await page.getByRole("button", { name: /^Collapse auto-research/ }).click();
    const header = page.locator(".campaign-run-heading");
    assert.equal(
      await header.locator(".status-pill").textContent(),
      "Wrapping up visualization and report",
    );
    projected = {
      ...projected,
      status: "needs_action",
      wrapup_state: "failed",
      live: false,
      wrapup_error: "The ending receipt could not be admitted.",
      health: "needs_action",
      recommendation: "reauthorize",
      blocked_reason: "reauthorize",
      can_reauthorize: true,
    };
    await page.evaluate(() => window.refreshMergeEpisode());
    await header.getByText("Needs action", { exact: true }).waitFor();
    // Newly available authorization opens the card; its collapsed header stays truthful too.
    await page.getByRole("button", { name: /^Collapse auto-research/ }).click();
    assert.equal(await header.locator(".status-pill").textContent(), "Needs action");
    assert.equal(await page.locator(".campaign-run-detail").count(), 0);
    await page.getByRole("button", { name: /^Expand auto-research/ }).click();
    await page
      .getByText("The authorized turns are spent. Authorize more turns", { exact: true })
      .waitFor();
    await page
      .getByText("Report generation error: The ending receipt could not be admitted.", {
        exact: true,
      })
      .waitFor();
    assert.equal(await page.getByText("Let auto-research continue", { exact: true }).count(), 0);
    assert.equal(polls, 2);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
  }
});

test("a served login notice parks verification, shows failure, then clears after success", async () => {
  const liveServer = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0, hmr: false },
  });
  let browser;
  try {
    await liveServer.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("requestfailed", (request) => errors.push(request.failure()?.errorText));
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().includes("409"))
        errors.push(message.text());
    });
    let finishProbe;
    let requests = 0;
    await page.route("**/api/providers/codex/logins/verify", async (route) => {
      requests += 1;
      assert.deepEqual(route.request().postDataJSON(), { host: "" });
      await new Promise((resolve) => {
        finishProbe = resolve;
      });
      await route.fulfill(
        requests === 1
          ? { status: 409, json: { detail: "Please sign in again" } }
          : { json: { state: { state: "signed_in", generation: 1 }, resumed: {} } },
      );
    });
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/providerLogin.html`,
    );
    await page.getByRole("button", { name: "Verify sign-in" }).click();
    assert.equal(await page.getByRole("button", { name: "Verifying…" }).isDisabled(), true);
    finishProbe();
    await page.getByRole("alert").waitFor();
    assert.equal(await page.getByRole("alert").textContent(), "Please sign in again");
    await page.getByRole("button", { name: "Verify sign-in" }).click();
    await page.getByRole("button", { name: "Verifying…" }).waitFor();
    finishProbe();
    await page.getByRole("status").waitFor({ state: "detached" });
    assert.equal(requests, 2);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
  }
});

test("an open space landing refreshes login notices with its Runs poll", async () => {
  const liveServer = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await liveServer.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("requestfailed", (request) => errors.push(request.failure()?.errorText));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    let states = [];
    let requests = 0;
    await page.route("**/api/providers/logins", (route) => {
      requests += 1;
      return route.fulfill({ json: states });
    });
    await page.route("**/api/team/connections", (route) => route.fulfill({ json: [] }));
    const firstLoad = page.waitForResponse("**/api/providers/logins");
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/appearance.html`,
    );
    await firstLoad;
    assert.equal(await page.getByRole("button", { name: "Verify sign-in" }).count(), 0);
    states = [
      {
        provider: "codex",
        host: "",
        state: "signed_out",
        generation: 0,
        changed_at: "2026-09-14T00:00:00Z",
        detail: "Please sign in again",
      },
    ];
    await page.evaluate(() => window.refreshSpaceRuns());
    await page.getByRole("button", { name: "Verify sign-in" }).waitFor();
    const timestamp = page.locator(".provider-login-notice time");
    assert.equal(await timestamp.getAttribute("datetime"), states[0].changed_at);
    assert.notEqual(await timestamp.textContent(), states[0].changed_at);
    assert.equal(requests, 2);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
  }
});
