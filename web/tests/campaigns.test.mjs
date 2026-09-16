import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import {
  rootTask,
  episode,
  branchId,
  baseHead,
  branchHead,
  withGraphBranch,
} from "./fixtures/campaigns.mjs";

import {
  archiveEpisode,
  loadEpisodeMessages,
  loadEpisodes,
  loadExperimentEpisodes,
  loadProjectExperimentEpisodes,
  loadSpaceRuns,
  mergeEpisodeToMain,
  continueEpisode,
  sendEpisodeMessage,
  startEpisode,
  stopEpisode,
} from "../src/api.ts";
import {
  episodeProjection,
  episodeReportPreviewUrl,
  episodeTaskRows,
  isLiveEpisode,
  mergeEpisode,
  runsEpisodeCards,
} from "../src/campaigns.ts";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { AutoResearchEpisodeCard } = await server.ssrLoadModule("/src/components/CampaignRuns.tsx");
const { EpisodeTimeline } = await server.ssrLoadModule("/src/components/EpisodeTimeline.tsx");
const { AutoResearchDialog } = await server.ssrLoadModule("/src/components/AutoResearchDialog.tsx");

after(() => server.close());

test("the Auto-research dialog meters only operational invocations", () => {
  const html = renderToStaticMarkup(
    React.createElement(AutoResearchDialog, {
      open: true,
      busy: false,
      error: null,
      initialInvocationCeiling: 1,
      onClose() {},
      onAuthorize() {},
    }),
  );

  assert.match(html, /Operational invocation ceiling/);
  assert.match(html, /type="number" min="1"/);
  assert.doesNotMatch(html, /reserved for the report|Report invocation/);
  assert.doesNotMatch(html, /Start auto-research" disabled/);
});

function renderEpisodes(values, { busyAction = null } = {}) {
  return renderToStaticMarkup(
    React.createElement(
      "section",
      {},
      values.map((value, index) =>
        React.createElement(AutoResearchEpisodeCard, {
          episode: value,
          tasks: values.flatMap((episode) => episode.tasks),
          messages: [],
          initiallyExpanded: index === 0 || value.live,
          busyAction,
          taskActionId: null,
          onInspectTask() {},
          async onLoadMessages() {},
          async onStop() {},
          async onMerge() {},
          async onContinue() {},
          async onSendMessage() {},
          async onOperateTask() {},
          onSwitchProvider() {},
          key: value.episode_id,
        }),
      ),
    ),
  );
}

test("the episode parent owns an operational-only invocation meter", () => {
  const html = renderEpisodes([episode]);

  assert.match(html, /Auto-research/);
  assert.match(html, /<time dateTime="2026-08-12T08:00:00Z">/);
  assert.doesNotMatch(html, /Episode ·|campaign-run-summary|Project episode/);
  assert.match(html, /3 \/ 8 invocations/);
  assert.match(html, /3 of 8 operational invocations used/);
  assert.doesNotMatch(html, /reserved|report unit|episode_report/i);
  assert.match(html, /12345|12,345/);
});

test("an episode turn that lost part of its launch says so on its timeline row", () => {
  const note = "Claude ignored the requested reasoning effort 'ultra' and ran at its own default.";
  const event = {
    event_id: `turn:${rootTask.operation_id}`,
    kind: "turn",
    at: rootTask.created_at,
    actor: { kind: "orchestrator", id: rootTask.operation_id, label: "Orchestrator", member: null },
    parent_event_id: null,
    title: "Orchestrator turn",
    detail: note,
    status: "succeeded",
    cause: null,
    links: {
      task_id: rootTask.operation_id,
      message_id: null,
      notice_id: null,
      episode_id: episode.episode_id,
      control_node_id: null,
    },
    provenance: "recorded",
  };
  const render = (detail) =>
    renderToStaticMarkup(
      React.createElement(EpisodeTimeline, {
        events: [{ ...event, detail }],
        apiBase: "/api/projects/demo",
        episodeId: episode.episode_id,
        onInspectTask() {},
      }),
    );
  const html = render(note);
  assert.match(html, /ignored the requested reasoning effort/);
  assert.match(html, /status-pill succeeded/);
  assert.doesNotMatch(render(null), /ignored the requested reasoning effort/);
});

test("Stop visibility consumes backend can_stop and preserves an in-flight Stop", () => {
  const backendStoppable = {
    ...episode,
    health: "failed",
    recommendation: "review",
    can_stop: true,
  };
  assert.match(renderEpisodes([backendStoppable]), />Stop<\/button>/);

  const requestInFlight = { ...episode, can_stop: false };
  assert.match(
    renderEpisodes([requestInFlight], { busyAction: `stop:${episode.episode_id}` }),
    />Stopping…<\/button>/,
  );
});

test("wrap-up has one exact parent state and no report task or recovery control", () => {
  const failedTask = {
    ...rootTask,
    status: "failed",
    status_message: "The operational turn ended",
    can_pause: false,
    can_retry: true,
  };
  const wrapping = {
    ...episode,
    status: "wrapping_up",
    wrapup_state: "running",
    tasks: [failedTask],
    health: "wrapping_up",
    recommendation: "wait",
    task_control: null,
  };
  const projection = episodeProjection(wrapping, wrapping.tasks);
  const html = renderEpisodes([wrapping]);

  assert.equal(projection.healthLabel, "Wrapping up visualization and report");
  assert.equal(projection.taskControl, null);
  assert.ok((html.match(/Wrapping up visualization and report/g) ?? []).length >= 2);
  assert.doesNotMatch(html, />Retry<|>Resume<|Report task|episode_report/);
});

test("only a control that can take a new binding offers the switch", () => {
  const failed = { ...rootTask, status: "failed", can_pause: false, can_retry: true };
  const worker = {
    ...failed,
    operation_id: "worker-turn",
    role: "worker",
    depth: 1,
    parent_operation_id: rootTask.operation_id,
  };
  const withControl = (task) => ({
    ...episode,
    status: "needs_action",
    health: "needs_action",
    recommendation: "retry",
    task_control: "retry",
    live: false,
    tasks: [{ ...failed, role: "orchestrator", depth: 0 }, worker].filter(
      (member) => member.operation_id === task.operation_id || member.role === "orchestrator",
    ),
    current_control_task_id: task.operation_id,
    current_operation_id: task.operation_id,
  });

  const orchestrator = withControl(failed);
  assert.equal(
    episodeProjection(orchestrator, orchestrator.tasks).taskControl.canSwitchProvider,
    true,
  );
  assert.match(renderEpisodes([orchestrator]), /Switch provider…/);

  // A worker continues only through the session its dispatch bound it to, so
  // every switch it could submit would come back refused.
  const workerControl = withControl(worker);
  assert.equal(
    episodeProjection(workerControl, workerControl.tasks).taskControl.canSwitchProvider,
    false,
  );
  assert.doesNotMatch(renderEpisodes([workerControl]), /Switch provider…/);

  // A stopping episode admits only exact recovery, from any actor, so the exact
  // Retry stays and the rebinding beside it goes.
  const stopping = { ...orchestrator, stop_requested_at: "2026-09-16T00:00:00Z" };
  assert.equal(episodeProjection(stopping, stopping.tasks).taskControl.canSwitchProvider, false);
  const html = renderEpisodes([stopping]);
  assert.doesNotMatch(html, /Switch provider…/);
  assert.match(html, />Retry</);
});

test("a ready episode exposes one singular report URL", () => {
  const ready = {
    ...episode,
    status: "completed",
    live: false,
    ending: "completed",
    ended_at: "2026-08-12T08:04:00Z",
    wrapup_state: "ready",
    report: {
      report_id: "internal-report-id",
      ending: "completed",
      created_at: "2026-08-12T08:04:00Z",
    },
    can_stop: false,
    can_message: false,
    tasks: [{ ...rootTask, status: "succeeded", can_pause: false }],
    health: "completed",
    recommendation: "open_report",
    task_control: null,
  };
  const html = renderEpisodes([ready]);

  assert.match(html, /> Open report<|>Open report</);
  assert.match(
    html,
    /href="\/api\/projects\/project%20one\/episodes\/episode%2Falpha\/report\/viewer"/,
  );
  assert.doesNotMatch(html, /internal-report-id/);
  assert.equal(
    episodeReportPreviewUrl("project one", "episode/alpha"),
    "/api/projects/project%20one/episodes/episode%2Falpha/report/viewer",
  );
});

test("a final report error is visible, terminal, and has no task recovery control", () => {
  const failedTask = {
    ...rootTask,
    status: "failed",
    can_pause: false,
    can_resume: true,
    can_retry: true,
  };
  const reportFailed = {
    ...episode,
    status: "needs_action",
    live: false,
    ending: "exhausted",
    wrapup_state: "failed",
    wrapup_error: "The visual report could not be written.",
    tasks: [failedTask],
    can_stop: false,
    can_message: false,
    can_continue: true,
    health: "needs_action",
    recommendation: "reauthorize",
    task_control: null,
  };
  const projection = episodeProjection(reportFailed, reportFailed.tasks);
  const html = renderEpisodes([reportFailed]);

  assert.equal(projection.taskControl, null);
  assert.match(html, /Report generation error: The visual report could not be written\./);
  assert.match(html, /Turns to add/);
  assert.doesNotMatch(html, />Retry<|>Resume<|Open report/);
});

test("a continuation chain is one run card listing each member's ceiling, ending, and report", () => {
  const sourceMember = {
    episode_id: "episode/source",
    created_at: "2026-08-11T08:00:00Z",
    status: "needs_action",
    ending: "exhausted",
    invocation_ceiling: 3,
    invocations_used: 3,
    report: { report_id: "report-1", ending: "exhausted", created_at: "2026-08-11T09:00:00Z" },
  };
  const source = {
    ...episode,
    episode_id: "episode/source",
    status: "needs_action",
    live: false,
    ending: "exhausted",
    can_continue: false,
    continued_by_episode_id: "episode/continuation",
    health: "needs_action",
    recommendation: "review",
    chain: [sourceMember],
  };
  const continuationMember = {
    episode_id: "episode/continuation",
    created_at: episode.created_at,
    status: "running",
    ending: null,
    invocation_ceiling: 2,
    invocations_used: 1,
    report: null,
  };
  const continuation = {
    ...episode,
    episode_id: "episode/continuation",
    continues_episode_id: "episode/source",
    can_continue: false,
    // The server publishes the whole chain; the card never rebuilds it from the list.
    chain: [sourceMember, continuationMember],
  };

  assert.deepEqual(runsEpisodeCards([source, continuation], new Set()), [continuation]);
  assert.deepEqual(runsEpisodeCards([continuation], new Set()), [continuation]);
  // Archiving the displayed newest member removes the whole run from default Runs.
  const archivedContinuation = { ...continuation, archived: true };
  assert.deepEqual(runsEpisodeCards([source, archivedContinuation], new Set()), []);
  assert.deepEqual(runsEpisodeCards([source, archivedContinuation], new Set(), true), [
    archivedContinuation,
  ]);
  const html = renderEpisodes([continuation]);

  assert.match(html, /Continued 1 time/);
  assert.match(html, /3 of 3 turns[^]*?Exhausted[^]*?1 of 2 turns[^]*?Current/);
  assert.match(html, new RegExp(`${encodeURIComponent("episode/source")}/report/viewer`));
  assert.doesNotMatch(html, /Continued by|Continues /);
  assert.doesNotMatch(html, /Turns to add/);
});

test("a report error does not downgrade a completed episode", () => {
  const reportFailed = {
    ...episode,
    status: "completed",
    live: false,
    ending: "completed",
    wrapup_state: "failed",
    wrapup_error: "The visual report could not be written.",
    tasks: [{ ...rootTask, status: "succeeded", can_pause: false }],
    can_stop: false,
    can_message: false,
    health: "completed",
    recommendation: "none",
    task_control: null,
  };
  const projection = episodeProjection(reportFailed, reportFailed.tasks);
  const html = renderEpisodes([reportFailed]);

  assert.equal(projection.health, "completed");
  assert.equal(projection.taskControl, null);
  assert.match(html, /Report generation error: The visual report could not be written\./);
  assert.doesNotMatch(html, />Retry<|>Resume<|Open report/);
});

test("Stop is the only ending that shows neither a report nor a report error", () => {
  const stopped = {
    ...episode,
    status: "stopped",
    live: false,
    ending: "stopped",
    ending_diagnostic: null,
    wrapup_state: "skipped",
    wrapup_error: null,
    report: null,
    can_stop: false,
    can_message: false,
    health: "stopped",
    recommendation: "none",
    task_control: null,
    tasks: [{ ...rootTask, status: "succeeded", can_pause: false }],
  };
  const html = renderEpisodes([stopped]);

  assert.match(html, /Stopped/);
  assert.doesNotMatch(html, /Open report|Report generation error|must stay hidden/);
});

test("reauthorization keeps the immutable old episode and inserts the fresh parent", () => {
  const oldEpisode = {
    ...episode,
    status: "needs_action",
    live: false,
    ending: "exhausted",
    wrapup_state: "ready",
    can_stop: false,
    can_continue: true,
  };
  const freshEpisode = {
    ...episode,
    episode_id: "episode/fresh",
    root_operation_id: "fresh-root",
    current_operation_id: "fresh-root",
    created_at: "2026-08-12T09:00:00Z",
  };

  assert.deepEqual(
    mergeEpisode([oldEpisode], freshEpisode).map((item) => item.episode_id),
    ["episode/fresh", "episode/alpha"],
  );
  assert.equal(isLiveEpisode(oldEpisode), false);
  assert.equal(isLiveEpisode(freshEpisode), true);
});

test("Runs keeps only the backend-selected current episode for each Experiment node", () => {
  const olderExperiment = {
    ...episode,
    episode_id: "experiment/older",
    mode: "experiment_loop",
    control_node_id: "exp/shared",
    created_at: "2026-08-10T08:00:00Z",
  };
  const newerExperiment = {
    ...olderExperiment,
    episode_id: "experiment/newer",
    created_at: "2026-08-12T09:00:00Z",
  };
  const otherExperiment = {
    ...olderExperiment,
    episode_id: "experiment/other",
    control_node_id: "exp/other",
    created_at: "2026-08-11T08:00:00Z",
  };
  const autoResearch = { ...episode, episode_id: "auto/one" };

  assert.deepEqual(
    runsEpisodeCards(
      [olderExperiment, autoResearch, otherExperiment, newerExperiment],
      new Set([olderExperiment.episode_id, otherExperiment.episode_id]),
    ).map((item) => item.episode_id),
    ["auto/one", "experiment/other", "experiment/older"],
  );
});

test("Show archived retains an archived Experiment after a newer episode replaces it", () => {
  const archived = { ...episode, mode: "experiment_loop", archived: true, episode_id: "old" };
  const current = { ...episode, mode: "experiment_loop", episode_id: "current" };
  const currentIds = new Set([current.episode_id]);
  assert.deepEqual(runsEpisodeCards([archived, current], currentIds), [current]);
  assert.deepEqual(
    new Set(runsEpisodeCards([archived, current], currentIds, true)),
    new Set([archived, current]),
  );
});

test("an eligible episode shows its graph branch base, head, and merge action", () => {
  const html = renderEpisodes([withGraphBranch()]);

  assert.match(html, /Episode graph branch/);
  assert.match(html, /Graph branch/);
  assert.match(html, /8ba94d42\u20260dd3b8/);
  assert.match(html, /Base on main/);
  assert.match(html, />r4</);
  assert.match(html, /Branch head/);
  assert.match(html, />r2</);
  assert.match(html, /Unmerged/);
  assert.match(html, />Merge to main</);
});

test("ineligible and running branches retain merge controls; an in-flight action disables them", () => {
  const ineligible = renderEpisodes([withGraphBranch({ merge_eligible: false })]);
  const running = renderEpisodes([
    withGraphBranch({
      merge_eligible: false,
      merge_state: "running",
      active_merge_task_id: "merge-task",
    }),
  ]);
  const disabled = renderEpisodes([withGraphBranch()], {
    busyAction: `stop:${episode.episode_id}`,
  });

  assert.match(running, /Merge running/);
  assert.match(ineligible, />Merge to main</);
  assert.match(running, />Merge to main</);
  assert.match(disabled, /<button[^>]+disabled=""[^>]*>.*Merge to main/s);
});

test("the merge control merges on branch facts and never ends the episode", () => {
  const html = renderEpisodes([withGraphBranch({ current_episode_id: "episode/alpha" })]);

  assert.match(html, />Merge to main</);
  assert.doesNotMatch(html, /End and merge/);
});

test("merged and failed branch summaries stay visible without branch-management controls", () => {
  const merged = renderEpisodes([
    withGraphBranch({
      merge_eligible: false,
      merge_state: "merged",
      latest_successful_merge: {
        schema_generation: 1,
        outcome: "committed",
        provenance: {
          schema_generation: 1,
          merge_id: "merge-1",
          branch_id: branchId,
          episode_id: episode.episode_id,
          branch_base_head: baseHead,
          branch_head: branchHead,
          rebased_main_head: { ...baseHead, revision: 10, transition_id: "main-before-0010" },
          merge_task_id: "merge-task",
        },
        result_main_head: { ...baseHead, revision: 11, transition_id: "main-after-0011" },
        authorized_by: { space_id: "space", user_id: "human", display_name: "Ada" },
        created_at: "2026-08-12T08:05:00Z",
      },
    }),
  ]);
  const failed = renderEpisodes([
    withGraphBranch({
      merge_state: "failed",
      merge_diagnostic: "The branch delta could not be rebased onto current main.",
    }),
  ]);

  assert.match(merged, />Merged</);
  assert.match(merged, /Merged on main/);
  assert.match(merged, />r11</);
  assert.match(merged, />Merge to main</);
  assert.doesNotMatch(merged, /discard|switch|conflict viewer/i);
  assert.match(failed, /Merge failed/);
  assert.match(failed, /The branch delta could not be rebased onto current main\./);
  assert.match(failed, />Merge to main</);
});

test("a paused or interrupted merge asks for action without presenting a failure", () => {
  const needsAction = renderEpisodes([
    withGraphBranch({
      merge_state: "needs_action",
      merge_diagnostic: "The merge was interrupted before it could finish.",
    }),
  ]);

  assert.match(needsAction, /campaign-graph-branch needs_action/);
  assert.match(needsAction, /Merge needs action/);
  assert.match(needsAction, /campaign-branch-diagnostic needs_action/);
  assert.match(needsAction, /The merge was interrupted before it could finish\./);
  assert.match(needsAction, />Merge to main</);
  assert.doesNotMatch(needsAction, /Merge failed|branch-failed|role="alert"/);
});

test("retries and continuations stay at their canonical actor depth", () => {
  const orchestratorRetryOne = {
    ...rootTask,
    operation_id: "turn-root-retry-1",
    parent_operation_id: rootTask.operation_id,
    created_at: "2026-08-12T08:01:00Z",
  };
  const orchestratorRetryTwo = {
    ...rootTask,
    operation_id: "turn-root-retry-2",
    parent_operation_id: orchestratorRetryOne.operation_id,
    created_at: "2026-08-12T08:02:00Z",
  };
  const worker = {
    ...rootTask,
    operation_id: "turn-worker",
    role: "worker",
    depth: 1,
    request: {
      role: "worker",
      actor_operation_id: "turn-worker",
      control_node_id: "experiment/demo",
    },
    parent_operation_id: orchestratorRetryTwo.operation_id,
    created_at: "2026-08-12T08:03:00Z",
  };
  const workerWake = {
    ...worker,
    operation_id: "turn-worker-wake",
    role: "wake",
    request: { ...worker.request, wake_cause: "message" },
    parent_operation_id: worker.operation_id,
    created_at: "2026-08-12T08:04:00Z",
  };
  const workerRetry = {
    ...worker,
    operation_id: "turn-worker-retry",
    parent_operation_id: workerWake.operation_id,
    created_at: "2026-08-12T08:05:00Z",
  };

  assert.deepEqual(
    episodeTaskRows(episode, [
      workerRetry,
      workerWake,
      worker,
      orchestratorRetryTwo,
      orchestratorRetryOne,
      rootTask,
    ]).map(({ task, role, depth }) => [task.operation_id, role, depth]),
    [
      ["turn-root", "orchestrator", 0],
      ["turn-root-retry-1", "orchestrator", 0],
      ["turn-root-retry-2", "orchestrator", 0],
      ["turn-worker", "worker", 1],
      ["turn-worker-wake", "wake", 1],
      ["turn-worker-retry", "worker", 1],
    ],
  );
});

test("episode API calls use only the generic endpoints and the continuation body", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, init = {}) => {
    requests.push({ path, method: init.method ?? "GET", body: init.body ?? null });
    const payload = path.endsWith("/messages")
      ? init.method === "POST"
        ? { message_id: "m" }
        : []
      : [];
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await loadEpisodes("/api/projects/demo", "auto_research");
    await loadEpisodes("/api/projects/demo", "auto_research", "episode/older");
    await startEpisode("/api/projects/demo", {
      mode: "auto_research",
      invocation_ceiling: 8,
      starting_instruction: "Start here",
    });
    await stopEpisode("/api/projects/demo", "episode/alpha");
    await archiveEpisode("/api/projects/demo", "episode/alpha", true);
    await archiveEpisode("/api/projects/demo", "episode/alpha", false);
    await continueEpisode("/api/projects/demo", "episode/alpha", 4, "request-1");
    await mergeEpisodeToMain("/api/projects/demo", "episode/alpha");
    await loadEpisodeMessages("/api/projects/demo", "episode/alpha");
    await sendEpisodeMessage("/api/projects/demo", "episode/alpha", "Check the blocker");
    await loadExperimentEpisodes();
    await loadProjectExperimentEpisodes("project/one");
    await loadSpaceRuns();
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(requests, [
    {
      path: "/api/projects/demo/episodes?mode=auto_research",
      method: "GET",
      body: null,
    },
    {
      path: "/api/projects/demo/episodes?mode=auto_research&episode_id=episode%2Folder",
      method: "GET",
      body: null,
    },
    {
      path: "/api/projects/demo/episodes",
      method: "POST",
      body: JSON.stringify({
        mode: "auto_research",
        invocation_ceiling: 8,
        starting_instruction: "Start here",
      }),
    },
    {
      path: "/api/projects/demo/episodes/episode%2Falpha/stop",
      method: "POST",
      body: null,
    },
    {
      path: "/api/projects/demo/episodes/episode%2Falpha/archive",
      method: "POST",
      body: JSON.stringify({ archived: true }),
    },
    {
      path: "/api/projects/demo/episodes/episode%2Falpha/archive",
      method: "POST",
      body: JSON.stringify({ archived: false }),
    },
    {
      path: "/api/projects/demo/episodes/episode%2Falpha/continue",
      method: "POST",
      body: JSON.stringify({ invocation_ceiling: 4, request_id: "request-1" }),
    },
    {
      path: "/api/projects/demo/episodes/episode%2Falpha/merge",
      method: "POST",
      body: null,
    },
    {
      path: "/api/projects/demo/episodes/episode%2Falpha/messages",
      method: "GET",
      body: null,
    },
    {
      path: "/api/projects/demo/episodes/episode%2Falpha/messages",
      method: "POST",
      body: JSON.stringify({ body: "Check the blocker" }),
    },
    { path: "/api/episodes?mode=experiment_loop", method: "GET", body: null },
    {
      path: "/api/projects/project%2Fone/experiment-episodes?mode=experiment_loop",
      method: "GET",
      body: null,
    },
    { path: "/api/space/runs", method: "GET", body: null },
  ]);
  assert.equal(
    requests.some(({ path }) => path.includes("campaign")),
    false,
  );
  assert.equal(
    requests.some(({ path }) => path.includes("experiment-loops")),
    false,
  );
});

for (const mode of ["auto_research", "experiment_loop"]) {
  for (const wrapupState of ["not_started", "failed"]) {
    test(`${mode} exhausted ${wrapupState} card uses the settled projection`, () => {
      const ended = {
        ...episode,
        mode,
        status: "needs_action",
        ending: "exhausted",
        wrapup_state: wrapupState,
        wrapup_error: wrapupState === "failed" ? "Receipt admission failed." : null,
        live: false,
        health: "needs_action",
        recommendation: "reauthorize",
        blocked_reason: "reauthorize",
        task_control: null,
        can_stop: false,
        can_message: false,
      };
      const projection = episodeProjection(ended);
      assert.equal(projection.healthLabel, "Needs action");
      assert.equal(projection.recommendation.label, "The authorized turns are spent. Add turns");
      const html = renderEpisodes([ended]);
      assert.match(html, /Needs action/);
      assert.doesNotMatch(html, /Let auto-research continue/);
      if (wrapupState === "failed")
        assert.match(html, /Report generation error: Receipt admission failed\./);
    });
  }
}

test("an exhausted episode waiting for admission says wrapping up", () => {
  const wrapping = {
    ...episode,
    status: "wrapping_up",
    ending: "exhausted",
    wrapup_state: "not_started",
    health: "wrapping_up",
    recommendation: "wait",
    blocked_reason: null,
    task_control: null,
  };
  assert.equal(episodeProjection(wrapping).health, "wrapping_up");
  const html = renderEpisodes([wrapping]);
  assert.match(html, /Wrapping up visualization and report/);
  assert.doesNotMatch(html, /Let auto-research continue/);
});

test("a blocked login leads the recovery instruction", () => {
  const blocked = {
    ...episode,
    health: "needs_action",
    recommendation: "retry",
    blocked_reason: "sign_in",
  };
  assert.equal(
    episodeProjection(blocked).recommendation.label,
    "The provider login is dead. Sign in again, then retry the current turn",
  );
});

test("an episode parked on a Decision says the choice is owed", () => {
  // The wake it armed can only be met by a human choosing on the episode's own
  // branch, which Inbox attention never covers. "Wait" alone would read as
  // progress the human need not touch.
  const parked = {
    ...episode,
    health: "active",
    recommendation: "wait",
    blocked_reason: null,
    awaiting_decision_ids: ["decision/scale"],
  };
  assert.match(
    episodeProjection(parked).recommendation.label,
    /^A Decision is waiting on your choice\./,
  );

  const several = { ...parked, awaiting_decision_ids: ["decision/scale", "decision/budget"] };
  assert.match(
    episodeProjection(several).recommendation.label,
    /^2 Decisions are waiting on your choice\./,
  );
});

test("a blocked login outranks a Decision the episode is parked on", () => {
  const blocked = {
    ...episode,
    health: "needs_action",
    recommendation: "retry",
    blocked_reason: "sign_in",
    awaiting_decision_ids: ["decision/scale"],
  };
  assert.equal(
    episodeProjection(blocked).recommendation.label,
    "The provider login is dead. Sign in again, then retry the current turn",
  );
});

test("the login notice names each signed-out account once and offers verification", async () => {
  const { ProviderLoginNotice } = await server.ssrLoadModule(
    "/src/components/ProviderLoginNotice.tsx",
  );
  const states = [
    {
      provider: "codex",
      host: "",
      state: "signed_out",
      generation: 0,
      changed_at: "2026-09-14T00:00:00Z",
      detail: "Please sign in again",
    },
    {
      provider: "claude",
      host: "remote.example",
      state: "signed_in",
      generation: 1,
      changed_at: "2026-09-14T00:00:00Z",
      detail: null,
    },
  ];
  const html = renderToStaticMarkup(React.createElement(ProviderLoginNotice, { states }));
  assert.match(html, /Codex is signed out\./);
  assert.match(html, /Please sign in again/);
  assert.match(html, /Sign it in from Settings, Provider logins/);
  assert.equal((html.match(/Check again/g) ?? []).length, 1);
  assert.doesNotMatch(html, /remote.example/);
});
