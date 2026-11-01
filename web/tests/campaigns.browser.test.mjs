import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { createServer } from "vite";
import { mockEpisodeReads, timelineFixture } from "./fixtures/timeline.mjs";
import { mockEpisodeArtifacts } from "./fixtures/episodeArtifacts.mjs";
import { chromium } from "playwright";
import { rootTask, episode, withGraphBranch } from "./fixtures/campaigns.mjs";

test("a failed merge stays Merge, and a refused click marks it at phone width", async () => {
  const liveServer = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await liveServer.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("requestfailed", (request) => errors.push(request.failure()?.errorText));
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().includes("409 (Conflict)"))
        errors.push(message.text());
    });
    const failure = "Stored merge diagnostic";
    const failed = {
      ...withGraphBranch({ merge_state: "failed", merge_diagnostic: failure }),
      can_merge: true,
      status: "completed",
      live: false,
      can_stop: false,
      can_message: false,
      task_control: null,
      tasks: [],
    };
    let polledEpisode = failed;
    await mockEpisodeArtifacts(page);
    await mockEpisodeReads(page);
    await page.route("**/api/projects/*/episodes/*/questions", (route) =>
      route.fulfill({ json: [] }),
    );
    await page.route("**/api/projects/**/timeline", (route) =>
      route.fulfill({ json: timelineFixture(episode.episode_id, episode.mode) }),
    );
    await page.route("**/fixture/episode", (route) => route.fulfill({ json: polledEpisode }));
    let previews = 0;
    const previewFailure = "Preview unavailable";
    await page.route("**/api/projects/**/merge-preview*", (route) => {
      previews += 1;
      return route.fulfill(
        previews === 1
          ? { status: 409, json: { detail: previewFailure } }
          : {
              json: {
                delivered_baseline: null,
                graph: { direct: 1, conflicts: 0, reviews: 0, paths: [] },
                code: null,
                needs_agent: false,
              },
            },
      );
    });
    const requests = [];
    const refusal = "Merge admission refused";
    await page.route("**/api/projects/**/merge", async (route) => {
      requests.push(route.request().postDataJSON());
      await route.fulfill(
        requests.length === 1
          ? { status: 409, json: { detail: refusal } }
          : {
              status: 202,
              json: {
                ...failed,
                can_merge: false,
                graph_branch: {
                  ...failed.graph_branch,
                  merge_state: "running",
                  merge_diagnostic: null,
                  active_merge_task_id: "fresh-merge-task",
                },
              },
            },
      );
    });
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/branchMerge.html`,
    );
    const branch = page.getByRole("region", { name: "Episode graph branch" });
    const button = branch.locator(".campaign-branch-merge");
    const titled = (reason) =>
      page.waitForFunction(
        (expected) => document.querySelector(".campaign-branch-merge")?.title === expected,
        reason,
      );
    await button.waitFor();
    // An earlier attempt's failure marks Merge without a caption.
    assert.equal(await button.getAttribute("title"), failure);
    assert.equal(await branch.getByText(failure, { exact: true }).count(), 0);
    await button.click();
    await titled(previewFailure);
    assert.equal(await branch.getByRole("group", { name: "Merge options" }).count(), 0);
    await button.click();
    const merge = branch
      .getByRole("group", { name: "Merge options" })
      .getByRole("button", { name: "Merge", exact: true });
    await merge.click();
    await titled(refusal);
    await page.evaluate(() => window.refreshMergeEpisode());
    assert.equal(await button.getAttribute("title"), refusal);
    assert.equal(await branch.getByText(refusal, { exact: true }).count(), 0);
    const box = await branch.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= 390));
    await button.click();
    await merge.click();
    await branch.locator(".branch-running").waitFor();
    assert.equal(requests.length, 2);
    assert.equal(requests[0].keep_branch_open, false);
    assert.deepEqual(requests[1], requests[0]);
    assert.equal(await button.count(), 0);
    polledEpisode = {
      ...failed,
      can_merge: false,
      graph_branch: {
        ...failed.graph_branch,
        merge_state: "merged",
        merge_eligible: false,
        merge_diagnostic: null,
      },
    };
    await page.evaluate(() => window.refreshMergeEpisode());
    await branch.locator(".branch-merged").waitFor();
    assert.equal(await button.count(), 0);
    polledEpisode = {
      ...failed,
      graph_branch: { ...failed.graph_branch, merge_state: "unmerged", merge_diagnostic: null },
    };
    await page.evaluate(() => window.refreshMergeEpisode());
    await button.waitFor();
    assert.equal(await button.getAttribute("title"), null);
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
    page.on("requestfailed", (request) => {
      // Collapsing the card releases the detail reads it opened; those aborts are deliberate.
      const path = new URL(request.url()).pathname;
      if (
        /\/episodes(\/[^/]+\/(messages|timeline|artifacts|questions))?$/.test(path) &&
        request.failure()?.errorText === "net::ERR_ABORTED"
      )
        return;
      errors.push(request.failure()?.errorText);
    });
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
    await mockEpisodeArtifacts(page);
    await mockEpisodeReads(page);
    await page.route("**/api/projects/*/episodes/*/questions", (route) =>
      route.fulfill({ json: [] }),
    );
    await page.route("**/api/projects/**/timeline", (route) =>
      route.fulfill({
        json: timelineFixture(episode.episode_id, episode.mode),
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
    assert.equal(await header.locator(".status-pill.wrapping_up").count(), 1);

    projected = {
      ...projected,
      status: "needs_action",
      wrapup_state: "failed",
      live: false,
      wrapup_error: "The ending receipt could not be admitted.",
      health: "needs_action",
      recommendation: "reauthorize",
      blocked_reason: "reauthorize",
      can_continue: true,
    };
    await page.evaluate(() => window.refreshMergeEpisode());
    await header.getByText("Needs action", { exact: true }).waitFor();
    // Newly available authorization opens the card; its collapsed header stays truthful too.
    await page.getByRole("button", { name: /^Collapse auto-research/ }).click();

    assert.equal(await header.locator(".status-pill.needs_action").count(), 1);
    assert.equal(await page.locator(".campaign-run-detail").count(), 0);
    await page.getByRole("button", { name: /^Expand auto-research/ }).click();
    await page.getByText("The authorized turns are spent. Add turns", { exact: true }).waitFor();
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

test("a served login notice is one row whose action opens its sign-ins, on a phone too", async () => {
  const liveServer = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0, hmr: false },
  });
  let browser;
  try {
    await liveServer.listen();
    browser = await chromium.launch({ headless: true });
    const errors = [];
    for (const viewport of [
      { width: 390, height: 844 },
      { width: 1280, height: 800 },
    ]) {
      const page = await browser.newPage({ viewport });
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      await page.goto(
        `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/providerLogin.html`,
      );
      const rows = page.locator("[data-login-notice]");
      await rows.first().waitFor();
      assert.deepEqual(
        await rows.evaluateAll((items) => items.map((item) => item.dataset.loginNotice)),
        ["open", "other-member"],
      );
      for (const row of await rows.all()) assert.ok((await row.boundingBox()).height < 60);
      assert.equal(await page.locator("[data-login-notice-action]").count(), 1);
      await page.locator('[data-login-notice-action="open-sign-ins"]').click();
      assert.deepEqual(await page.evaluate(() => window.openedSignIns), [
        { provider: "codex", host: "" },
      ]);
      const width = await page.evaluate(() => [
        document.documentElement.scrollWidth,
        document.documentElement.clientWidth,
      ]);
      assert.ok(width[0] <= width[1]);
      await page.close();
    }
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
    await page.route("**/api/machine-power", (route) =>
      route.fulfill({ json: { platform: "linux", supported: false } }),
    );
    const firstLoad = page.waitForResponse("**/api/providers/logins");
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/appearance.html`,
    );
    await firstLoad;
    assert.equal(await page.locator("[data-login-notice]").count(), 0);
    states = [
      {
        provider: "codex",
        host: "",
        login_id: "",
        kind: "shared",
        owner_user_id: null,
        state: "signed_out",
        generation: 0,
        changed_at: "2026-09-14T00:00:00Z",
        detail: "Please sign in again",
        agent_used: true,
      },
    ];
    await page.evaluate(() => window.refreshSpaceRuns());
    await page.locator('[data-login-notice="open"]').waitFor();
    assert.equal(requests, 2);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
  }
});

test("an envelope loads full text, reports fetch failure, and closes accessibly", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 400, height: 800 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("requestfailed", (request) => errors.push(request.failure()?.errorText));
    page.on("console", (message) => {
      if (message.type() === "error" && !message.text().includes("503 (Service Unavailable)"))
        errors.push(message.text());
    });
    await page.route("**/fixture/episode", (route) => route.fulfill({ json: episode }));
    const actor = (actor_id, kind, label) => ({
      actor_id,
      kind,
      label,
      row_key: actor_id,
      owner_episode_id: episode.episode_id,
      started_at: "2026-09-24T10:00:00Z",
      ended_at: "2026-09-24T11:00:00Z",
      outcome: "succeeded",
      subtitle: null,
      started_by_span_id: null,
      links: {},
    });
    await mockEpisodeArtifacts(page);
    await mockEpisodeReads(page);
    await page.route("**/api/projects/*/episodes/*/questions", (route) =>
      route.fulfill({ json: [] }),
    );
    await page.route("**/api/projects/**/timeline", (route) =>
      route.fulfill({
        json: timelineFixture(episode.episode_id, episode.mode, {
          members: [
            {
              episode_id: episode.episode_id,
              started_at: "2026-09-24T10:00:00Z",
              ended_at: "2026-09-24T11:00:00Z",
              continues_episode_id: null,
            },
          ],
          actors: [
            actor("human:one", "human", "Human"),
            actor("orchestrator:one", "orchestrator", "Orchestrator"),
          ],
          messages: [
            {
              item_id: "message:one",
              from_actor_id: "human:one",
              to_actor_id: "orchestrator:one",
              sent_at: "2026-09-24T10:10:00Z",
              sent_span_id: null,
              delivered_at: "2026-09-24T10:12:00Z",
              delivered_span_id: null,
              disposition: "harvested",
              preview: "Compare the latest results.",
              text_ref: "message:one",
            },
          ],
        }),
      }),
    );
    let textRequests = 0;
    let failText = false;
    let releaseText;
    const textReady = new Promise((resolve) => {
      releaseText = resolve;
    });
    const body = "Compare the latest results. Full archived instructions include the control run.";
    await page.route("**/timeline/text/*", async (route) => {
      textRequests += 1;
      await textReady;
      if (failText)
        return route.fulfill({ status: 503, json: { detail: "Text temporarily unavailable" } });
      assert.equal(
        decodeURIComponent(new URL(route.request().url()).pathname).split("/").at(-1),
        "message:one",
      );
      return route.fulfill({
        json: {
          text_ref: "message:one",
          kind: "message",
          owner_episode_id: episode.episode_id,
          body,
          sha256: "a".repeat(64),
        },
      });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/branchMerge.html`,
    );
    const timeline = page.getByRole("region", { name: "Episode timeline" });
    const message = timeline.getByRole("button", {
      name: "Message from Human to Orchestrator",
      exact: true,
    });
    await message.click();
    const card = page.getByRole("dialog");
    await card.getByRole("status").waitFor();
    releaseText();
    await card.getByText(body, { exact: true }).waitFor();
    assert.equal(textRequests, 1);
    await page.keyboard.press("Escape");
    await card.waitFor({ state: "detached" });
    failText = true;
    await message.focus();
    await page.keyboard.press("Enter");
    await card.getByRole("alert").waitFor();
    await page.getByRole("button", { name: "Whole run", exact: true }).click();
    await card.waitFor({ state: "detached" });
    failText = false;
    await message.click();
    await card.getByText(body, { exact: true }).waitFor();
    await card.getByRole("button", { name: "Close", exact: true }).click();
    await card.waitFor({ state: "detached" });
    const envelope = await message.locator(".roster-envelope").boundingBox();
    assert.ok(envelope);
    const x = envelope.x + envelope.width / 2;
    const y = envelope.y + envelope.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + 35, y, { steps: 5 });
    await page.mouse.move(x, y, { steps: 5 });
    await page.mouse.up();
    assert.equal(await card.count(), 0, "dragging over an envelope must not open it");
    assert.equal(textRequests, 3);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});

test("ended orchestrator message sends the edited budget and retains its id after a lost response", async () => {
  const cacheDir = await mkdtemp(join(tmpdir(), "rcp-message-test-"));
  const liveServer = await createServer({
    root: new URL("..", import.meta.url).pathname,
    cacheDir,
    configLoader: "runner",
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
    const ended = {
      ...episode,
      live: false,
      status: "completed",
      can_message: true,
      can_stop: false,
      can_continue: true,
      message_requires_continuation: true,
    };
    await mockEpisodeArtifacts(page);
    await mockEpisodeReads(page);
    await page.route("**/api/projects/*/episodes/*/questions", (route) =>
      route.fulfill({ json: [] }),
    );
    await page.route("**/api/projects/**/timeline", (route) =>
      route.fulfill({ json: timelineFixture(episode.episode_id, episode.mode) }),
    );
    await page.route("**/fixture/episode", (route) => route.fulfill({ json: ended }));
    const requests = [];
    await page.route("**/api/projects/**/messages", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      requests.push(route.request().postDataJSON());
      await route.fulfill(
        requests.length === 1
          ? { status: 503, json: { detail: "lost" } }
          : { json: { message_id: "message", episode_id: "continued" } },
      );
    });
    await page.goto(
      `http://127.0.0.1:${liveServer.httpServer.address().port}/tests/fixtures/branchMerge.html`,
    );
    const composer = page.locator(".campaign-message-composer");
    const turns = composer.locator('input[type="number"]');
    assert.equal(await turns.inputValue(), "3");
    await turns.fill("1");
    await composer.locator("textarea").fill("Investigate");
    await composer.locator('button[type="submit"]').click();
    await page.locator(".campaign-run-error").waitFor();
    ended.message_requires_continuation = false;
    await page.evaluate(() => window.refreshMergeEpisode());
    await turns.waitFor({ state: "detached" });
    await composer.locator('button[type="submit"]').click();
    await page.waitForFunction(
      () => document.querySelector(".campaign-message-composer textarea").value === "",
    );
    assert.equal(requests.length, 2);
    assert.equal(requests[0].invocation_ceiling, 1);
    assert.equal(requests[0].body, "Investigate");
    assert.ok(requests[0].request_id);
    assert.deepEqual(requests[1], requests[0]);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await liveServer.close();
    await rm(cacheDir, { recursive: true, force: true });
  }
});
