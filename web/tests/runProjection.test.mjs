import assert from "node:assert/strict";
import test from "node:test";

import {
  buildExperimentRun,
  buildRunProjection,
  buildRunTaskProjection,
  experimentRunSection,
} from "../src/runProjection.ts";

function task(
  operationId,
  status,
  createdAt,
  parentOperationId = null,
  { kind = "refresh", request = {} } = {},
) {
  return {
    operation_id: operationId,
    project_id: "project",
    kind,
    status,
    request,
    created_at: createdAt,
    updated_at: createdAt,
    status_message: `${operationId} ${status}`,
    attempt: 1,
    parent_operation_id: parentOperationId,
    phase: "agent",
    last_activity_at: createdAt,
  };
}

function loopTask(operationId, nodeId, episodeId, status, createdAt, parentOperationId = null) {
  return task(operationId, status, createdAt, parentOperationId, {
    kind: "node_chat",
    request: {
      patch_kind: "experiment_loop",
      control_node_id: nodeId,
      control_episode_id: episodeId,
      control_invocation: 1,
    },
  });
}

function experiment(id, status = "planned", fields = {}) {
  return {
    id,
    type: "experiment",
    title: `Experiment ${id}`,
    extension_fields: {},
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    status,
    objective: "Measure the bounded loop.",
    attempts: [],
    invocation_ceiling: 3,
    completion_criteria: [],
    ...fields,
  };
}

function control(fields = {}, operationalFields = {}) {
  return {
    ready: true,
    reasons: [],
    invocations_used: 0,
    invocation_ceiling: 3,
    invocations_remaining: 3,
    episode_id: null,
    paused: false,
    active: false,
    governing_decisions: [],
    decision_drift: [],
    operational: {
      task_active: false,
      detached_work_active: false,
      watcher_degraded: false,
      watcher_completion_pending: false,
      episode_exited: false,
      stop_requested: false,
      stop_settled: false,
      chat_id: null,
      current_operation_id: null,
      current_status: null,
      current_phase: null,
      current_status_message: null,
      current_last_activity_at: null,
      current_invocation: null,
      session: {
        provider: null,
        model: null,
        reasoning: null,
        run_on: null,
        execution_host: null,
        run_truth_scope: null,
        native_session_bound: false,
        diagnostic: null,
      },
      ...operationalFields,
    },
    ...fields,
  };
}

function watcher(id, nodeId, episodeId, status, fields = {}) {
  return {
    watcher_id: id,
    project_id: "project",
    origin_operation_id: `origin-${id}`,
    chat_id: "chat",
    node_id: nodeId,
    execution_host: "",
    check_command: "true",
    log_path: `/tmp/${id}.log`,
    cwd: "/tmp",
    continuation: {
      patch_kind: "experiment_loop",
      control_node_id: nodeId,
      control_episode_id: episodeId,
      control_invocation: 1,
      control_invocation_ceiling: 3,
    },
    status,
    created_at: "2026-08-06T01:00:00Z",
    last_checked_at: null,
    last_exit_code: null,
    last_error: null,
    completed_at: null,
    notified: false,
    notification_operation_id: null,
    ...fields,
  };
}

function byId(projection) {
  return new Map([
    ...projection.running.map((entry) => [entry.id, ["running", entry]]),
    ...projection.actionable.map((entry) => [entry.id, ["actionable", entry]]),
    ...projection.completed.map((entry) => [entry.id, ["completed", entry]]),
  ]);
}

test("task retries group under their logical root and classify by the latest attempt", () => {
  const projection = buildRunTaskProjection([
    task("root", "failed", "2026-07-28T00:00:00Z"),
    task("retry-1", "failed", "2026-07-28T01:00:00Z", "root"),
    task("retry-2", "running", "2026-07-28T02:00:00Z", "retry-1"),
    task("paused", "paused", "2026-07-28T03:00:00Z"),
    task("done", "succeeded", "2026-07-28T04:00:00Z"),
  ]);

  assert.deepEqual(
    projection.running[0].attempts.map((item) => item.operation_id),
    ["root", "retry-1", "retry-2"],
  );
  assert.equal(projection.running[0].latest.operation_id, "retry-2");
  assert.deepEqual(
    projection.actionable.map((group) => group.rootId),
    ["paused"],
  );
  assert.deepEqual(
    projection.completed.map((group) => group.rootId),
    ["done"],
  );
});

test("dismissed and superseded ingestion failures leave the action queue", () => {
  const failed = task("failed", "failed", "2026-07-28T00:00:00Z");
  const laterSuccess = task("later", "succeeded", "2026-07-28T01:00:00Z");
  const dismissed = task("dismissed", "failed", "2026-07-28T02:00:00Z");
  const projection = buildRunTaskProjection(
    [laterSuccess, dismissed, failed],
    new Set(["dismissed"]),
  );
  assert.deepEqual(projection.actionable, []);
  assert.deepEqual(
    projection.completed.map((group) => group.rootId),
    ["later"],
  );
});

test("Runs includes Experiment-loop tasks once and excludes generic chat and coach tasks", () => {
  const node = experiment("experiment/include");
  const loop = loopTask("loop-task", node.id, "episode-current", "running", "2026-08-06T03:00:00Z");
  const projection = buildRunProjection({
    nodes: [node],
    tasks: [
      loop,
      task("generic-chat", "running", "2026-08-06T04:00:00Z", null, {
        kind: "node_chat",
        request: { patch_kind: "work" },
      }),
      task("coach", "failed", "2026-08-06T05:00:00Z", null, {
        kind: "paper_coach",
      }),
      task("refresh", "running", "2026-08-06T02:00:00Z"),
    ],
    experimentControl: {
      [node.id]: control(
        { episode_id: "episode-current", invocations_used: 1, invocations_remaining: 2 },
        {
          task_active: true,
          current_operation_id: loop.operation_id,
          current_status: "running",
        },
      ),
    },
  });

  assert.deepEqual(
    projection.running.map((entry) => [entry.kind, entry.id]),
    [
      ["experiment", node.id],
      ["task", "refresh"],
    ],
  );
  assert.equal(projection.actionable.length, 0);
});

test("Experiment projection follows operational precedence and stop task placement", () => {
  const nodes = [
    experiment("terminal-live", "completed"),
    experiment("stopping-live"),
    experiment("stopping-failed"),
    experiment("healthy-wait"),
    experiment("degraded-wait"),
    experiment("ceiling-pending"),
    experiment("graph-gated"),
    experiment("session-unavailable"),
    experiment("human-stopped"),
    experiment("terminal", "superseded"),
  ];
  const controls = {
    "terminal-live": control(
      { episode_id: "ep-terminal-live", invocations_used: 1, invocations_remaining: 2 },
      { task_active: true, current_operation_id: "terminal-live-task", current_status: "running" },
    ),
    "stopping-live": control(
      {
        ready: false,
        reasons: ["A graceful stop is finishing the current loop turn."],
        episode_id: "ep-stopping-live",
        invocations_used: 1,
        invocations_remaining: 2,
      },
      {
        task_active: true,
        stop_requested: true,
        current_operation_id: "stopping-live-task",
        current_status: "pausing",
      },
    ),
    "stopping-failed": control(
      {
        ready: false,
        reasons: ["A graceful stop is finishing the current loop turn."],
        episode_id: "ep-stopping-failed",
        invocations_used: 1,
        invocations_remaining: 2,
      },
      {
        task_active: true,
        stop_requested: true,
        current_operation_id: "stopping-failed-task",
        current_status: "failed",
      },
    ),
    "healthy-wait": control(
      {
        ready: false,
        reasons: ["An experiment loop is already active."],
        episode_id: "ep-healthy",
        invocations_used: 1,
        invocations_remaining: 2,
        active: true,
      },
      { detached_work_active: true },
    ),
    "degraded-wait": control(
      {
        ready: false,
        reasons: ["An experiment loop is already active."],
        episode_id: "ep-degraded",
        invocations_used: 1,
        invocations_remaining: 2,
        active: true,
      },
      { detached_work_active: true },
    ),
    "ceiling-pending": control(
      {
        episode_id: "ep-ceiling",
        invocations_used: 3,
        invocations_remaining: 0,
        paused: true,
      },
      { watcher_completion_pending: true },
    ),
    "graph-gated": control(
      {
        ready: false,
        reasons: ["Blocker blocker/upstream is open."],
        episode_id: "ep-gated",
        invocations_used: 1,
        invocations_remaining: 2,
      },
      { detached_work_active: true },
    ),
    "session-unavailable": control(
      {
        episode_id: "ep-session",
        invocations_used: 1,
        invocations_remaining: 2,
      },
      {
        watcher_completion_pending: true,
        session: {
          ...control().operational.session,
          diagnostic: "The bound native session is unavailable.",
        },
      },
    ),
    "human-stopped": control(
      {
        episode_id: "ep-stopped",
        invocations_used: 1,
        invocations_remaining: 2,
      },
      { stop_requested: true, stop_settled: true },
    ),
    terminal: control(),
  };
  const tasks = [
    loopTask(
      "terminal-live-task",
      "terminal-live",
      "ep-terminal-live",
      "running",
      "2026-08-06T01:00:00Z",
    ),
    loopTask(
      "stopping-live-task",
      "stopping-live",
      "ep-stopping-live",
      "pausing",
      "2026-08-06T02:00:00Z",
    ),
    loopTask(
      "stopping-failed-task",
      "stopping-failed",
      "ep-stopping-failed",
      "failed",
      "2026-08-06T03:00:00Z",
    ),
  ];
  const watchers = [
    watcher("healthy", "healthy-wait", "ep-healthy", "active"),
    watcher("degraded", "degraded-wait", "ep-degraded", "degraded"),
    watcher("ceiling", "ceiling-pending", "ep-ceiling", "completed"),
    watcher("gated", "graph-gated", "ep-gated", "active"),
    watcher("session", "session-unavailable", "ep-session", "completed"),
  ];
  const entries = byId(buildRunProjection({ nodes, tasks, watchers, experimentControl: controls }));

  assert.deepEqual(
    entries.get("terminal-live").map((value, index) => (index ? value.experiment.health : value)),
    ["running", "agent_active"],
  );
  assert.deepEqual(
    entries.get("stopping-live").map((value, index) => (index ? value.experiment.health : value)),
    ["running", "stopping"],
  );
  assert.deepEqual(
    entries.get("stopping-failed").map((value, index) => (index ? value.experiment.health : value)),
    ["actionable", "stopping"],
  );
  assert.deepEqual(
    entries.get("healthy-wait").map((value, index) => (index ? value.experiment.health : value)),
    ["running", "waiting_on_watchers"],
  );
  assert.deepEqual(
    entries.get("degraded-wait").map((value, index) => (index ? value.experiment.health : value)),
    ["running", "degraded"],
  );
  assert.deepEqual(
    entries.get("ceiling-pending").map((value, index) => (index ? value.experiment.health : value)),
    ["actionable", "paused_at_limit"],
  );
  assert.equal(entries.get("graph-gated")[0], "actionable");
  assert.equal(entries.get("session-unavailable")[0], "actionable");
  assert.deepEqual(
    entries.get("human-stopped").map((value, index) => (index ? value.experiment.health : value)),
    ["actionable", "human_stopped"],
  );
  assert.deepEqual(
    entries.get("terminal").map((value, index) => (index ? value.experiment.health : value)),
    ["completed", "completed"],
  );
  assert.equal(experimentRunSection("stopping", "interrupted"), "actionable");
  assert.equal(experimentRunSection("stopping", "running"), "running");
});

test("historical watchers stay visible without driving current health or task selection", () => {
  const node = experiment("experiment/history");
  const current = loopTask(
    "current-task",
    node.id,
    "episode-current",
    "succeeded",
    "2026-08-06T01:00:00Z",
  );
  const newerHistory = loopTask(
    "historical-task",
    node.id,
    "episode-history",
    "failed",
    "2026-08-06T05:00:00Z",
  );
  const run = buildExperimentRun(
    node,
    control(
      { episode_id: "episode-current", invocations_used: 1, invocations_remaining: 2 },
      { current_operation_id: current.operation_id, current_status: "succeeded" },
    ),
    [newerHistory, current],
    [watcher("old-degraded", node.id, "episode-history", "degraded")],
  );

  assert.equal(run.watchers.length, 1);
  assert.equal(run.currentWatchers.length, 0);
  assert.equal(run.currentTask.operation_id, "current-task");
  assert.equal(run.health, "needs_action");
});

test("compatible adopted degraded watchers drive current health through control state", () => {
  const node = experiment("experiment/adopted-degraded");
  const current = loopTask(
    "current-task",
    node.id,
    "episode-current",
    "succeeded",
    "2026-08-06T01:00:00Z",
  );
  const run = buildExperimentRun(
    node,
    control(
      {
        ready: false,
        reasons: ["An experiment loop is already active."],
        episode_id: "episode-current",
        invocations_used: 1,
        invocations_remaining: 2,
      },
      {
        current_operation_id: current.operation_id,
        current_status: "succeeded",
        detached_work_active: true,
        watcher_degraded: true,
      },
    ),
    [current],
    [watcher("adopted-degraded", node.id, "episode-older", "degraded")],
  );

  assert.equal(run.currentWatchers.length, 0);
  assert.equal(run.health, "degraded");
});

test("entries remain newest first within each section", () => {
  const projection = buildRunProjection({
    nodes: [],
    tasks: [
      task("older", "running", "2026-08-06T01:00:00Z"),
      task("newer", "running", "2026-08-06T02:00:00Z"),
    ],
  });
  assert.deepEqual(
    projection.running.map((entry) => entry.id),
    ["newer", "older"],
  );
});
