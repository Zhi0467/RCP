import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import {
  loadEpisodeMessages,
  loadEpisodes,
  loadExperimentEpisodes,
  reauthorizeEpisode,
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
} from "../src/campaigns.ts";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { AutoResearchEpisodes } = await server.ssrLoadModule("/src/components/CampaignRuns.tsx");
const { AutoResearchDialog } = await server.ssrLoadModule("/src/components/AutoResearchDialog.tsx");

after(() => server.close());

const rootTask = {
  operation_id: "turn-root",
  project_id: "project one",
  kind: "auto_research",
  status: "running",
  request: { role: "orchestrator", actor_operation_id: "turn-root" },
  created_at: "2026-08-12T08:00:00Z",
  updated_at: "2026-08-12T08:00:00Z",
  status_message: "Reviewing the graph",
  attempt: 1,
  parent_operation_id: null,
  episode_id: "episode/alpha",
  estimate_seconds: 10,
  estimate_samples: 1,
  phase: "running",
  elapsed_seconds: 1,
  progress: 0.3,
  can_pause: true,
  can_resume: false,
  can_retry: false,
};

const episode = {
  episode_id: "episode/alpha",
  project_id: "project one",
  mode: "auto_research",
  control_node_id: null,
  root_operation_id: rootTask.operation_id,
  current_operation_id: rootTask.operation_id,
  current_orchestrator_task_id: rootTask.operation_id,
  current_control_task_id: rootTask.operation_id,
  recovery: null,
  status: "running",
  starting_instruction: "Begin with the unresolved **Blocker**.",
  budget: {
    invocation_ceiling: 8,
    invocations_used: 3,
    invocations_remaining: 5,
    observed_input_tokens: 12_345,
    observed_generated_tokens: 678,
  },
  authorized_by: { space_id: "space", user_id: "human", display_name: "Ada" },
  stop_requested_at: null,
  ending: null,
  ending_diagnostic: null,
  wrapup_state: "not_started",
  wrapup_error: null,
  created_at: "2026-08-12T08:00:00Z",
  updated_at: "2026-08-12T08:02:00Z",
  ended_at: null,
  tasks: [rootTask],
  report: null,
  can_stop: true,
  can_reauthorize: false,
};

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

function renderEpisodes(values, tasks = values.flatMap((value) => value.tasks)) {
  return renderToStaticMarkup(
    React.createElement(AutoResearchEpisodes, {
      episodes: values,
      tasks,
      messagesByEpisode: {},
      busyAction: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadMessages() {},
      async onStop() {},
      async onReauthorize() {},
      async onSendMessage() {},
      async onOperateTask() {},
    }),
  );
}

test("the episode parent owns an operational-only invocation meter", () => {
  const html = renderEpisodes([episode]);

  assert.match(html, /Auto-research episodes/);
  assert.match(html, /Project episode/);
  assert.match(html, /3 \/ 8 invocations/);
  assert.match(html, /3 of 8 operational invocations used/);
  assert.doesNotMatch(html, /reserved|report unit|episode_report/i);
  assert.match(html, /12345|12,345/);
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
  };
  const projection = episodeProjection(wrapping, wrapping.tasks);
  const html = renderEpisodes([wrapping]);

  assert.equal(projection.healthLabel, "Wrapping up visualization and report");
  assert.equal(projection.taskControl, null);
  assert.ok((html.match(/Wrapping up visualization and report/g) ?? []).length >= 2);
  assert.doesNotMatch(html, />Retry<|>Resume<|Report task|episode_report/);
});

test("a ready episode exposes one singular report URL", () => {
  const ready = {
    ...episode,
    status: "completed",
    ending: "completed",
    ended_at: "2026-08-12T08:04:00Z",
    wrapup_state: "ready",
    report: {
      report_id: "internal-report-id",
      ending: "completed",
      created_at: "2026-08-12T08:04:00Z",
    },
    can_stop: false,
    tasks: [{ ...rootTask, status: "succeeded", can_pause: false }],
  };
  const html = renderEpisodes([ready]);

  assert.match(html, /> Open report<|>Open report</);
  assert.match(
    html,
    /href="\/api\/projects\/project%20one\/episodes\/episode%2Falpha\/report\/preview"/,
  );
  assert.doesNotMatch(html, /internal-report-id/);
  assert.equal(
    episodeReportPreviewUrl("project one", "episode/alpha"),
    "/api/projects/project%20one/episodes/episode%2Falpha/report/preview",
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
    ending: "exhausted",
    wrapup_state: "failed",
    wrapup_error: "The visual report could not be written.",
    tasks: [failedTask],
    can_stop: false,
    can_reauthorize: true,
  };
  const projection = episodeProjection(reportFailed, reportFailed.tasks);
  const html = renderEpisodes([reportFailed]);

  assert.equal(projection.taskControl, null);
  assert.match(html, /Report generation error: The visual report could not be written\./);
  assert.match(html, /New episode invocation ceiling/);
  assert.doesNotMatch(html, />Retry<|>Resume<|Open report/);
});

test("a report error does not downgrade a completed episode", () => {
  const reportFailed = {
    ...episode,
    status: "completed",
    ending: "completed",
    wrapup_state: "failed",
    wrapup_error: "The visual report could not be written.",
    tasks: [{ ...rootTask, status: "succeeded", can_pause: false }],
    can_stop: false,
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
    ending: "stopped",
    ending_diagnostic: "must stay hidden after Stop",
    wrapup_state: "skipped",
    wrapup_error: "must stay hidden after Stop",
    report: null,
    can_stop: false,
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
    ending: "exhausted",
    wrapup_state: "ready",
    can_stop: false,
    can_reauthorize: true,
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

test("episode API calls use only the generic endpoints and new-parent reauthorization body", async () => {
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
    await startEpisode("/api/projects/demo", {
      mode: "auto_research",
      invocation_ceiling: 8,
      starting_instruction: "Start here",
    });
    await stopEpisode("/api/projects/demo", "episode/alpha");
    await reauthorizeEpisode("/api/projects/demo", "episode/alpha", 4);
    await loadEpisodeMessages("/api/projects/demo", "episode/alpha");
    await sendEpisodeMessage("/api/projects/demo", "episode/alpha", "Check the blocker");
    await loadExperimentEpisodes();
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
      path: "/api/projects/demo/episodes/episode%2Falpha/reauthorize",
      method: "POST",
      body: JSON.stringify({ invocation_ceiling: 4 }),
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
