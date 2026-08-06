import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { ExperimentRunDetail } = await server.ssrLoadModule(
  "/src/components/ExperimentRunDetail.tsx",
);

after(() => server.close());

function node(fields = {}) {
  return {
    id: "experiment/detail",
    type: "experiment",
    title: "Detailed bounded experiment",
    extension_fields: {},
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    status: "running",
    objective: "Measure future plasticity.",
    current_summary: "The detached evaluation is still running.",
    next_action: "Inspect the held-out evaluation.",
    invocation_ceiling: 3,
    completion_criteria: ["All held-out evaluations have mechanically exited."],
    attempts: [
      {
        id: "attempt-1",
        sequence: 1,
        purpose: "Run the held-out evaluation",
        attempt_kind: "external_run",
        decision_bundle: [],
        status: "running",
        outcome: null,
        failure_reason: null,
        job_refs: ["slurm-48192"],
      },
    ],
    ...fields,
  };
}

function operational(fields = {}) {
  return {
    task_active: false,
    detached_work_active: false,
    watcher_completion_pending: false,
    episode_exited: false,
    stop_requested: false,
    stop_settled: false,
    chat_id: "chat-1",
    current_operation_id: null,
    current_status: null,
    current_phase: null,
    current_status_message: null,
    current_last_activity_at: "2026-08-06T04:00:00Z",
    current_invocation: 3,
    session: {
      provider: "codex",
      model: "gpt-5.6",
      reasoning: "high",
      run_on: "cluster",
      execution_host: "login.research",
      run_truth_scope: ["repo-a", "repo-b"],
      native_session_bound: true,
      diagnostic: null,
    },
    ...fields,
  };
}

function control(fields = {}, operationalFields = {}) {
  return {
    ready: true,
    reasons: [],
    invocations_used: 3,
    invocation_ceiling: 3,
    invocations_remaining: 0,
    episode_id: "episode-1",
    paused: true,
    active: false,
    governing_decisions: [
      { decision_id: "decision/resources", decision_revision: 7, selected_option: "4xA100" },
    ],
    decision_drift: [
      {
        decision_id: "decision/data",
        pinned_option: "v1",
        pinned_revision: 4,
        current_option: "v2",
        current_status: "decided",
        proposed: false,
      },
    ],
    operational: operational(operationalFields),
    ...fields,
  };
}

function watcher(fields = {}) {
  return {
    watcher_id: "watcher-1",
    project_id: "project",
    origin_operation_id: "origin-turn",
    chat_id: "chat-1",
    node_id: "experiment/detail",
    execution_host: "login.research",
    check_command: "jobs=$(squeue -h -j 48192 -o '%i') || exit 2; [ -z \"$jobs\" ]",
    log_path: "/scratch/evaluation.log",
    cwd: "/scratch/run",
    continuation: {
      patch_kind: "experiment_loop",
      control_node_id: "experiment/detail",
      control_episode_id: "episode-1",
      control_invocation: 3,
      control_invocation_ceiling: 3,
    },
    status: "completed",
    created_at: "2026-08-06T01:00:00Z",
    last_checked_at: "2026-08-06T04:00:00Z",
    last_exit_code: 0,
    last_error: null,
    completed_at: "2026-08-06T04:00:00Z",
    notified: false,
    notification_operation_id: null,
    ...fields,
  };
}

function render(run) {
  return renderToStaticMarkup(
    React.createElement(ExperimentRunDetail, {
      run,
      runBusy: false,
      runDisabled: false,
      stopBusy: false,
      onRun() {},
      onStopLoop() {},
      onInspectTask() {},
    }),
  );
}

test("completed watcher at the ceiling leaves human Run enabled", () => {
  const completed = watcher();
  const html = render({
    node: node(),
    control: control({}, { watcher_completion_pending: true }),
    taskGroup: null,
    currentTask: null,
    watchers: [completed],
    currentWatchers: [completed],
    health: "paused_at_limit",
  });

  assert.match(html, /Paused at invocation limit/);
  assert.match(html, /Run pending wake/);
  assert.doesNotMatch(html, /<button[^>]*disabled=""[^>]*>.*Run pending wake<\/button>/s);
  assert.match(html, /Stop loop/);
  assert.doesNotMatch(html, />Pause<|>Resume<|>Retry<|Stop watching/);
});

test("detail separates watcher provenance from semantic meaning and execution binding", () => {
  const stopped = watcher({
    status: "stopped",
    notified: true,
    completed_at: null,
    last_exit_code: null,
  });
  const html = render({
    node: node({ status: "planned" }),
    control: control(
      { invocations_used: 1, invocations_remaining: 2, paused: false },
      {
        stop_requested: true,
        stop_settled: true,
        current_operation_id: "authoritative-current-task",
      },
    ),
    taskGroup: null,
    currentTask: null,
    watchers: [stopped],
    currentWatchers: [stopped],
    health: "human_stopped",
  });

  assert.match(html, /role="status" aria-live="polite"/);
  assert.match(html, /Human-stopped/);
  assert.match(html, /Stopped · not delivered/);
  assert.match(html, /origin-turn/);
  assert.match(html, /episode episode-1 · invocation 3 \/ 3/);
  assert.match(html, /squeue -h -j 48192/);
  assert.match(html, /evaluation\.log/);
  assert.match(html, /Working directory/);
  assert.match(html, /codex/);
  assert.match(html, /gpt-5\.6/);
  assert.match(html, /high/);
  assert.match(html, /cluster · login\.research/);
  assert.match(html, /repo-a, repo-b/);
  assert.match(html, /Bound/);
  assert.match(html, /authoritative-current-task/);
  assert.match(html, /Completion criteria/);
  assert.match(html, /All held-out evaluations have mechanically exited/);
  assert.match(html, /Semantic attempts/);
  assert.match(html, /Run the held-out evaluation/);
  assert.match(html, /slurm-48192/);
  assert.match(html, /Governing decisions/);
  assert.match(html, /decision\/resources/);
  assert.match(html, /Decision drift/);
  assert.match(html, /decision\/data moved to v2/);
  assert.doesNotMatch(html, />Pause<|>Resume<|>Retry<|Stop watching/);
});

test("a queued notification claim is not presented as proven provider delivery", () => {
  const claimed = watcher({
    notified: true,
    notification_operation_id: "wake-task",
  });
  const html = render({
    node: node(),
    control: control(),
    taskGroup: null,
    currentTask: null,
    watchers: [claimed],
    currentWatchers: [claimed],
    health: "needs_action",
  });

  assert.match(html, /Delivery claimed/);
  assert.doesNotMatch(html, />Delivered</);
});
