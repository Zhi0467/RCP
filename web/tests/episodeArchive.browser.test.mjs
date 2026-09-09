import assert from "node:assert/strict";
import test from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

const ada = { space_id: "team", user_id: "ada", display_name: "Ada Lovelace" };
const grace = { space_id: "team", user_id: "grace", display_name: "Grace Hopper" };
const main = { kind: "main", branch_id: null };
const branch = { kind: "branch", branch_id: "parent" };

function episode(fields) {
  return {
    episode_id: "parent",
    project_id: "project-one",
    mode: "auto_research",
    control_node_id: null,
    graph_target: branch,
    graph_base_head: null,
    graph_branch: null,
    root_operation_id: null,
    current_operation_id: null,
    current_orchestrator_task_id: null,
    current_control_task_id: null,
    recovery: null,
    status: "failed",
    starting_instruction: null,
    budget: {
      invocation_ceiling: 3,
      invocations_used: 1,
      invocations_remaining: 2,
      observed_input_tokens: 1200,
      observed_generated_tokens: 400,
    },
    authorized_by: ada,
    stop_requested_at: null,
    ending: "failed",
    ending_diagnostic: null,
    wrapup_state: "legacy_unavailable",
    wrapup_error: null,
    created_at: "2026-09-01T12:00:00Z",
    updated_at: "2026-09-01T13:00:00Z",
    ended_at: "2026-09-01T13:00:00Z",
    tasks: [],
    report: null,
    can_stop: false,
    can_reauthorize: false,
    can_message: false,
    archived: false,
    can_archive: true,
    live: false,
    health: "failed",
    recommendation: "review",
    task_control: null,
    run_section: "actionable",
    ...fields,
  };
}

function node(id, title) {
  return {
    id,
    title,
    type: "experiment",
    status: "running",
    objective: "Measure transfer.",
    extension_fields: {},
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    design: "",
    expected_outcomes: [],
    interpretation_rules: [],
    completion_criteria: [],
    invocation_ceiling: 3,
    attempts: [],
    current_summary: "",
    next_action: null,
    current_summary_stale: false,
    next_action_stale: false,
  };
}

function control(episode) {
  return {
    ready: true,
    reasons: [],
    graph_reasons: [],
    invocations_used: 1,
    invocation_ceiling: 3,
    invocations_remaining: 2,
    episode_id: episode.episode_id,
    episode,
    paused: false,
    active: episode.live,
    stop_pending: false,
    governing_decisions: [],
    decision_drift: [],
    health: episode.live ? "waiting_on_watchers" : episode.health,
    recommendation: episode.live ? "wait" : "review",
    run_section: episode.run_section,
    live: episode.live,
    can_start: false,
    can_stop: episode.can_stop,
    can_open_report: false,
    can_switch_provider: false,
    node_closed: false,
    task_control: null,
    report_episode_id: null,
    operational: {
      task_active: false,
      detached_work_active: episode.live,
      watcher_degraded: false,
      watcher_completion_pending: false,
      episode_exited: !episode.live,
      episode_live: episode.live,
      stop_requested: false,
      stop_settled: false,
      chat_id: null,
      current_operation_id: null,
      current_status: null,
      current_queued: false,
      current_active: false,
      current_awaiting_human: false,
      current_phase: null,
      current_status_message: null,
      current_last_activity_at: null,
      current_invocation: 1,
      session: {
        provider: "codex",
        model: null,
        reasoning: null,
        run_on: "local",
        execution_host: "local",
        run_truth_scope: null,
        native_session_bound: true,
        diagnostic: null,
      },
    },
  };
}

test("active and unresolved episodes archive without stopping work and restore across shared run views", async () => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0, strictPort: false },
  });
  let browser;
  try {
    await server.listen();
    const address = server.httpServer.address();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    page.on("requestfailed", (request) => errors.push(request.failure()?.errorText));
    const nodes = {
      main: node("experiment/main", "Measure transfer"),
      child: node("experiment/child", "Reproduce the baseline"),
    };
    const records = [
      episode({ status: "needs_action", health: "needs_action" }),
      episode({
        episode_id: "child",
        mode: "experiment_loop",
        control_node_id: nodes.child.id,
        status: "running",
        health: "active",
        recommendation: "wait",
        run_section: "running",
        live: true,
        can_stop: true,
        ended_at: null,
        ending: null,
      }),
      episode({
        episode_id: "main-current",
        mode: "experiment_loop",
        control_node_id: nodes.main.id,
        graph_target: main,
        authorized_by: grace,
        status: "needs_action",
        health: "needs_action",
        run_section: "actionable",
      }),
      episode({
        episode_id: "main-old",
        mode: "experiment_loop",
        control_node_id: nodes.main.id,
        graph_target: main,
        archived: true,
        authorized_by: null,
        created_at: "2026-08-01T12:00:00Z",
        run_section: "completed",
      }),
      episode({
        episode_id: "active",
        graph_target: main,
        authorized_by: grace,
        status: "running",
        health: "active",
        recommendation: "wait",
        run_section: "running",
        live: true,
        can_stop: true,
        ended_at: null,
        ending: null,
      }),
      episode({
        episode_id: "branch-old",
        mode: "experiment_loop",
        control_node_id: nodes.main.id,
        graph_target: branch,
        archived: true,
        created_at: "2026-08-01T12:00:00Z",
        run_section: "completed",
      }),
    ];
    const snapshot = {
      graph: {
        revision: 1,
        nodes: { [nodes.main.id]: nodes.main },
        edges: {},
        proposals: {},
        ambiguities: {},
        glossary: {},
        validation_messages: [],
        belief_transitions: [],
        replay_status: "complete",
        replay_failure: null,
        ontology: { types: [], fields: [], relations: [] },
      },
      // The shared graph cache intentionally has no current archive eligibility.
      experiment_control: { [nodes.main.id]: control({ ...records[2], can_archive: false }) },
    };
    const entries = () =>
      [records[1], records[2]].map((item) => ({
        project_id: "project-one",
        project_name: "Team research",
        project_reachable: true,
        graph_target: item.graph_target,
        graph_head: null,
        parent_episode_id: item.episode_id === "child" ? "parent" : null,
        parent_watching: false,
        node: item.episode_id === "child" ? nodes.child : nodes.main,
        control: control(item),
        episode: item,
      }));
    const spaceEntries = () =>
      records.map((item) => ({
        episode_id: item.episode_id,
        project_id: item.project_id,
        project_name: "Team research",
        project_reachable: true,
        mode: item.mode,
        title:
          item.episode_id === "branch-old"
            ? "Archived branch experiment"
            : item.control_node_id === nodes.child.id
              ? nodes.child.title
              : item.control_node_id
                ? nodes.main.title
                : "Auto-research",
        graph_target: item.graph_target,
        parent_episode_id: item.episode_id === "child" ? "parent" : null,
        experiment_id: item.control_node_id,
        started_at: item.created_at,
        last_activity_at: item.updated_at,
        health_label: item.live ? "Active" : "Failed",
        health_tone: item.live ? "running" : "actionable",
        run_section: item.run_section,
        archived: item.archived,
        can_archive: item.can_archive,
        authorized_by: item.authorized_by,
      }));
    const writes = [];
    const mutationPaths = [];
    let holdReads = false;
    let notifyHeld;
    const held = [];
    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() !== "GET") mutationPaths.push(url.pathname);
      const archiveMatch = url.pathname.match(/\/episodes\/([^/]+)\/archive$/);
      if (archiveMatch) {
        const item = records.find(
          (record) => record.episode_id === decodeURIComponent(archiveMatch[1]),
        );
        assert.equal(request.method(), "POST");
        const body = request.postDataJSON();
        assert.deepEqual(Object.keys(body), ["archived"]);
        const { archived } = body;
        writes.push([item.episode_id, archived]);
        item.archived = archived;
        await route.fulfill({ json: item });
        return;
      }
      const isEpisodes = url.pathname === "/api/projects/project-one/episodes";
      const isIndex = url.pathname.endsWith("/experiment-episodes");
      const isSpace = url.pathname === "/api/space/runs";
      const payload = structuredClone(
        isEpisodes
          ? records
          : isIndex
            ? entries()
            : isSpace
              ? spaceEntries()
              : url.pathname.endsWith("/messages")
                ? []
                : snapshot,
      );
      if (holdReads && (isEpisodes || isIndex || isSpace)) {
        held.push(() => route.fulfill({ json: payload }));
        if (held.length === 3) {
          holdReads = false;
          notifyHeld();
        }
      } else {
        await route.fulfill({ json: payload });
      }
    });
    await page.goto(`http://127.0.0.1:${address.port}/tests/fixtures/episodeArchive.html`);
    const project = page.locator('[data-surface="project"]');
    const space = page.locator('[data-surface="space"]');
    const projectCard = (id) => project.locator(`[data-episode-id="${id}"]`);
    const spaceCard = (id) => space.locator(`[data-episode-id="${id}"]`);
    await project.getByRole("link", { name: /Reproduce the baseline/ }).waitFor();
    await projectCard("child").getByRole("button", { name: "Archive", exact: true }).waitFor();
    await projectCard("main-current")
      .getByRole("button", { name: "Archive", exact: true })
      .waitFor();
    await projectCard("active").getByRole("button", { name: "Archive", exact: true }).waitFor();
    await projectCard("parent").getByRole("button", { name: "Archive", exact: true }).waitFor();
    assert.equal(
      await projectCard("parent").locator('[title="Started by Ada Lovelace"]').count(),
      1,
    );
    assert.equal(
      await projectCard("child").locator('[title="Started by Ada Lovelace"]').count(),
      1,
    );
    assert.equal(
      await projectCard("main-current").locator('[title="Started by Grace Hopper"]').count(),
      1,
    );
    assert.equal(await projectCard("main-old").count(), 0);

    await projectCard("active").getByRole("button", { name: "Archive", exact: true }).click();
    await projectCard("active").waitFor({ state: "detached" });
    await spaceCard("active").waitFor({ state: "detached" });
    await project.getByRole("checkbox", { name: "Show archived" }).check();
    await space.getByRole("checkbox", { name: "Show archived" }).check();
    await projectCard("active").getByRole("button", { name: "Unarchive", exact: true }).waitFor();
    assert.equal(
      await projectCard("active").locator(".campaign-run-meta .status-pill").textContent(),
      "Active",
    );
    await projectCard("active")
      .getByRole("button", { name: /Expand auto-research episode/ })
      .click();
    assert.equal(
      await projectCard("active").getByRole("button", { name: "Stop", exact: true }).isEnabled(),
      true,
    );
    await spaceCard("active").getByRole("button", { name: "Unarchive", exact: true }).click();
    await projectCard("active").getByRole("button", { name: "Archive", exact: true }).waitFor();
    assert.equal(records[4].live, true);
    assert.equal(records[4].status, "running");
    assert.equal(records[4].stop_requested_at, null);
    await project.getByRole("checkbox", { name: "Show archived" }).uncheck();
    await space.getByRole("checkbox", { name: "Show archived" }).uncheck();

    // A Needs Action parent can be hidden while its child continues independently.
    await spaceCard("parent").getByRole("button", { name: "Archive", exact: true }).click();
    await projectCard("parent").waitFor({ state: "detached" });
    await spaceCard("parent").waitFor({ state: "detached" });
    assert.equal(await projectCard("child").count(), 1);
    assert.equal(await spaceCard("child").count(), 1);
    assert.equal(records[1].live, true);
    assert.equal(records[1].archived, false);
    await project.getByRole("checkbox", { name: "Show archived" }).check();
    await projectCard("parent").getByRole("button", { name: "Unarchive", exact: true }).click();
    await spaceCard("parent").getByRole("button", { name: "Archive", exact: true }).waitFor();
    await project.getByRole("checkbox", { name: "Show archived" }).uncheck();
    await project.getByRole("link", { name: /Reproduce the baseline/ }).waitFor();

    holdReads = true;
    const readsHeld = new Promise((resolve) => {
      notifyHeld = resolve;
    });
    await page.getByRole("button", { name: "Refresh runs", exact: true }).click();
    await readsHeld;
    await projectCard("child").getByRole("button", { name: "Archive", exact: true }).click();
    await projectCard("child").waitFor({ state: "detached" });
    await spaceCard("child").waitFor({ state: "detached" });
    assert.equal(await project.getByRole("link", { name: /Reproduce the baseline/ }).count(), 0);
    assert.equal(await projectCard("parent").count(), 1);
    await Promise.all(held.map((release) => release()));
    await page.evaluate(() => new Promise(requestAnimationFrame));
    assert.equal(await projectCard("child").count(), 0);
    assert.equal(await spaceCard("child").count(), 0);

    await project.getByRole("checkbox", { name: "Show archived" }).check();
    await projectCard("child").getByRole("button", { name: "Unarchive", exact: true }).waitFor();
    assert.equal(
      await projectCard("child").locator(".campaign-run-meta .status-pill").textContent(),
      "Waiting on watchers",
    );
    assert.equal(records[1].live, true);
    assert.equal(records[1].stop_requested_at, null);
    await projectCard("main-old").getByRole("button", { name: "Unarchive", exact: true }).waitFor();
    assert.equal(await projectCard("main-old").locator(".episode-author").count(), 0);
    assert.match(await projectCard("main-old").textContent(), /Measure transfer/);
    // A historical branch without an indexed node cannot borrow main's same-id title.
    assert.equal(
      await projectCard("branch-old").locator(".campaign-run-title").textContent(),
      nodes.main.id,
    );
    assert.doesNotMatch(await projectCard("branch-old").textContent(), /Measure transfer/);
    await projectCard("main-old").getByRole("button", { name: "Open History" }).click();
    await page.getByRole("dialog", { name: "Project History" }).waitFor();
    assert.equal(await project.locator(".needs-action > header > span").textContent(), "2");
    assert.equal(await project.locator(".completed > header > span").textContent(), "0");

    await space.getByRole("checkbox", { name: "Show archived" }).check();
    await spaceCard("child").getByRole("button", { name: "Unarchive", exact: true }).click();
    await projectCard("child").getByRole("button", { name: "Archive", exact: true }).waitFor();
    await project.getByRole("checkbox", { name: "Show archived" }).uncheck();
    await project.getByRole("link", { name: /Reproduce the baseline/ }).waitFor();
    await projectCard("main-current").getByRole("button", { name: "Archive", exact: true }).click();
    await projectCard("main-current").waitFor({ state: "detached" });
    assert.equal(await projectCard("child").count(), 1);
    assert.deepEqual(writes, [
      ["active", true],
      ["active", false],
      ["parent", true],
      ["parent", false],
      ["child", true],
      ["child", false],
      ["main-current", true],
    ]);

    // A teammate archives the unresolved parent while its child is still live.
    records[0].archived = true;
    await page.getByRole("button", { name: "Refresh runs", exact: true }).click();
    await projectCard("parent").waitFor({ state: "detached" });
    assert.equal(await projectCard("child").count(), 1);
    assert.equal(records[1].live, true);

    await page.goto(
      `http://127.0.0.1:${address.port}/tests/fixtures/episodeArchive.html#/projects/project-one?view=runs&experiment=experiment%2Fmain&episode=main-old&target=main`,
    );
    await page.reload();
    await projectCard("main-old").getByRole("button", { name: "Unarchive", exact: true }).waitFor();
    assert.equal(await project.getByRole("checkbox", { name: "Show archived" }).isChecked(), true);
    assert.equal(await projectCard("main-current").count(), 0);
    await projectCard("main-old").getByRole("button", { name: "Unarchive", exact: true }).click();
    await projectCard("main-old").waitFor({ state: "detached" });

    records[2].archived = false;
    records[2].can_archive = true;
    await page.goto(
      `http://127.0.0.1:${address.port}/tests/fixtures/episodeArchive.html#/projects/project-one?view=runs&experiment=experiment%2Fmain&episode=main-current&target=main`,
    );
    await page.reload();
    await projectCard("main-current").waitFor({ state: "attached" });
    const showArchived = project.getByRole("checkbox", { name: "Show archived" });
    assert.equal(await showArchived.isChecked(), false);

    // A teammate archives the selected run without changing this viewer's route.
    records[2].archived = true;
    await page.getByRole("button", { name: "Refresh runs", exact: true }).click();
    await projectCard("main-current")
      .getByRole("button", { name: "Unarchive", exact: true })
      .waitFor();
    assert.equal(await showArchived.isChecked(), true);

    // The viewer can hide it again; unchanged polling must respect that choice.
    await showArchived.uncheck();
    const refresh = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/projects/project-one/episodes",
    );
    await page.getByRole("button", { name: "Refresh runs", exact: true }).click();
    await (await refresh).finished();
    await page.evaluate(() => new Promise(requestAnimationFrame));
    assert.equal(await showArchived.isChecked(), false);
    assert.equal(await projectCard("main-current").count(), 0);
    assert.equal(records[4].live, true);
    assert.deepEqual(
      mutationPaths,
      writes.map(([episodeId]) => `/api/projects/project-one/episodes/${episodeId}/archive`),
      "Archive and unarchive never call stop, watcher, or other mutation endpoints",
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
