import assert from "node:assert/strict";
import { withExperimentControlAnswers, withTaskAnswers } from "./taskAnswers.mjs";
import test from "node:test";

import {
  buildExperimentRun,
  experimentRecommendation,
  experimentWatcherDisplayItems,
  graphConditionLabel,
  isGraphWatcherRecord,
  visibleChatWatchers,
  watcherIsActive,
  watcherIsIndividuallyStoppable,
  watcherLastObservedAt,
} from "../src/runProjection.ts";

function task(
  operationId,
  status,
  createdAt,
  parentOperationId = null,
  { kind = "refresh", request = {} } = {},
) {
  return withTaskAnswers({
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
  });
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
  return withExperimentControlAnswers({
    ready: true,
    reasons: [],
    graph_reasons: [],
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
      episode_live: false,
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
  });
}

function watcher(id, nodeId, episodeId, status, fields = {}) {
  return {
    watcher_id: id,
    project_id: "project",
    origin_operation_id: `origin-${id}`,
    origin_task_kind: "node_chat",
    chat_id: "chat",
    node_id: nodeId,
    execution_host: "",
    check_command: "true",
    log_path: `/tmp/${id}.log`,
    cwd: "/tmp",
    continuation: {
      provider: "codex",
      model: null,
      reasoning: null,
      run_on: "local",
      run_truth_scope: null,
      patch_kind: "experiment_loop",
      control_node_id: nodeId,
      control_revision: 1,
      control_episode_id: episodeId,
      control_invocation: 1,
      control_invocation_ceiling: 3,
      control_decision_bundle: [],
      control_completion_criteria: [],
      workflow_ids: [],
      skill_ids: [],
      invoked_workflow_ids: [],
      invoked_skill_ids: [],
      resolved_skill_packages: [],
    },
    status,
    created_at: "2026-08-06T01:00:00Z",
    last_checked_at: null,
    last_exit_code: null,
    last_error: null,
    completed_at: null,
    next_check_at: null,
    consecutive_error_count: 0,
    group_id: null,
    group_label: null,
    notified: false,
    notification_operation_id: null,
    stopped_by: null,
    stop_reason: null,
    stopped_at: null,
    stop_operation_id: null,
    ...fields,
  };
}

function graphWatcher(id, nodeId, episodeId, status, condition, fields = {}) {
  const external = watcher(id, nodeId, episodeId, status, fields);
  const {
    check_command: _checkCommand,
    log_path: _logPath,
    cwd: _cwd,
    last_checked_at: _lastCheckedAt,
    last_exit_code: _lastExitCode,
    last_error: _lastError,
    next_check_at: _nextCheckAt,
    consecutive_error_count: _consecutiveErrorCount,
    group_id: _groupId,
    group_label: _groupLabel,
    ...shared
  } = external;
  return {
    ...shared,
    condition,
    armed_revision: fields.armed_revision ?? 1,
    last_evaluated_at: fields.last_evaluated_at ?? null,
  };
}

test("backend operation identity selects the exact task even when a newer row exists", () => {
  const node = experiment("experiment/retry-active");
  const episodeId = "episode-retry-active";
  const failed = {
    ...loopTask("failed-attempt", node.id, episodeId, "failed", "2026-08-14T09:50:00Z"),
    can_retry: true,
  };
  const retry = {
    ...loopTask(
      "running-retry",
      node.id,
      episodeId,
      "running",
      "2026-08-14T09:51:00Z",
      failed.operation_id,
    ),
    attempt: 2,
  };
  const staleControl = control(
    {
      ready: false,
      reasons: ["An experiment loop is already active."],
      episode_id: episodeId,
      invocations_used: 3,
      invocations_remaining: 7,
      health: "needs_action",
      recommendation: "review",
      run_section: "actionable",
      can_start: false,
    },
    {
      task_active: true,
      current_operation_id: failed.operation_id,
      current_status: "failed",
      current_invocation: 3,
    },
  );

  const run = buildExperimentRun(node, staleControl, [failed, retry], []);
  assert.equal(run.currentTask.operation_id, failed.operation_id);
  assert.equal(run.health, "needs_action");
  assert.deepEqual(experimentRecommendation(run), {
    step: "review",
    label: "Review the loop state",
  });
});

test("Experiment watcher projection keeps each immutable group and ungrouped history distinct", () => {
  const nodeId = "experiment/grouped";
  const episodeId = "episode-grouped";
  const run = buildExperimentRun(
    experiment(nodeId),
    control({ episode_id: episodeId }),
    [],
    [
      watcher("ungrouped", nodeId, episodeId, "completed"),
      watcher("shard-finished", nodeId, episodeId, "completed", {
        group_id: "group-eval-shards",
        group_label: "eval-shards",
      }),
      watcher("shard-degraded", nodeId, episodeId, "degraded", {
        group_id: "group-eval-shards",
        group_label: "eval-shards",
      }),
      watcher("shard-running", nodeId, episodeId, "active", {
        group_id: "group-eval-shards",
        group_label: "eval-shards",
      }),
      watcher("shard-retired", nodeId, episodeId, "stopped", {
        group_id: "group-eval-shards",
        group_label: "eval-shards",
        stopped_by: "agent",
      }),
    ],
  );

  const grouped = run.watcherItems.find((item) => item.kind === "group");
  assert.ok(grouped && grouped.kind === "group");
  assert.equal(grouped.group.groupId, "group-eval-shards");
  assert.equal(grouped.group.label, "eval-shards");
  assert.deepEqual(grouped.group.counts, {
    finished: 1,
    degraded: 1,
    running: 1,
    stopped: 1,
  });
  assert.deepEqual(
    grouped.group.watchers.map((item) => item.watcher_id),
    ["shard-degraded", "shard-finished", "shard-retired", "shard-running"],
  );
  assert.deepEqual(
    run.watcherItems
      .filter((item) => item.kind === "watcher")
      .map((item) => item.watcher.watcher_id),
    ["ungrouped"],
  );
});

test("graph watchers stay ungrouped and expose condition labels and evaluation time", () => {
  const status = graphWatcher(
    "graph-status",
    "experiment/grouped",
    "episode-grouped",
    "active",
    { node_id: "blk/upstream", status_in: ["resolved", "superseded"] },
    { last_evaluated_at: "2026-08-06T03:00:00Z" },
  );
  const proposal = graphWatcher(
    "graph-proposal",
    "experiment/grouped",
    "episode-grouped",
    "active",
    { node_id: "hyp/result", proposal_resolved: true },
  );

  assert.equal(isGraphWatcherRecord(status), true);
  assert.equal(
    graphConditionLabel(status.condition),
    "blk/upstream reaches resolved or superseded",
  );
  assert.equal(graphConditionLabel(proposal.condition), "Proposal on hyp/result is resolved");
  assert.equal(watcherLastObservedAt(status), "2026-08-06T03:00:00Z");
  assert.deepEqual(
    visibleChatWatchers([status], "new-chat", experiment("experiment/grouped")).map(
      (watcher) => watcher.watcher_id,
    ),
    ["graph-status"],
  );
  assert.deepEqual(
    experimentWatcherDisplayItems([status, proposal]).map((item) => item.kind),
    ["watcher", "watcher"],
  );
});

test("Chats project node-owned loop watchers separately from conversation self-wake watchers", () => {
  const node = experiment("experiment/shared-loop");
  const episodeId = "episode-shared-loop";
  const loopActive = watcher("loop-active", node.id, episodeId, "active", {
    chat_id: "creator-chat",
  });
  const loopDegraded = watcher("loop-degraded", node.id, episodeId, "degraded", {
    chat_id: "other-creator-chat",
  });
  const loopStopped = watcher("loop-stopped", node.id, episodeId, "stopped", {
    chat_id: "creator-chat",
  });
  const loopCompleted = watcher("loop-completed", node.id, episodeId, "completed", {
    chat_id: "creator-chat",
  });
  const otherNodeLoop = watcher("other-node-loop", "experiment/other", "episode-other", "active", {
    chat_id: "creator-chat",
  });
  const selfWake = watcher("self-wake", null, null, "active", {
    chat_id: "maintenance-chat",
    continuation: {
      ...loopActive.continuation,
      patch_kind: "work",
      control_node_id: null,
      control_episode_id: null,
    },
  });
  const otherChatSelfWake = watcher("other-chat-self-wake", null, null, "active", {
    chat_id: "other-chat",
    continuation: {
      ...loopActive.continuation,
      patch_kind: "work",
      control_node_id: null,
      control_episode_id: null,
    },
  });
  const stoppedSelfWake = watcher("stopped-self-wake", null, null, "stopped", {
    chat_id: "maintenance-chat",
    continuation: selfWake.continuation,
  });
  const watchers = [
    loopActive,
    loopDegraded,
    loopStopped,
    loopCompleted,
    otherNodeLoop,
    selfWake,
    otherChatSelfWake,
    stoppedSelfWake,
  ];

  const sameNodeChat = visibleChatWatchers(watchers, "maintenance-chat", node);
  assert.deepEqual(
    sameNodeChat.map((item) => item.watcher_id),
    ["loop-active", "loop-degraded", "self-wake"],
  );
  assert.deepEqual(
    visibleChatWatchers([...watchers, loopActive, selfWake], "maintenance-chat", node).map(
      (item) => item.watcher_id,
    ),
    ["loop-active", "loop-degraded", "self-wake"],
  );
  const run = buildExperimentRun(node, control({ episode_id: episodeId }), [], watchers);
  assert.equal(
    sameNodeChat.filter((item) => item.continuation.patch_kind === "experiment_loop").length,
    run.watchers.filter(watcherIsActive).length,
  );

  assert.deepEqual(
    visibleChatWatchers(watchers, "maintenance-chat", null).map((item) => item.watcher_id),
    ["self-wake"],
  );
  assert.deepEqual(
    visibleChatWatchers(watchers, "maintenance-chat", experiment("experiment/unrelated")).map(
      (item) => item.watcher_id,
    ),
    ["self-wake"],
  );
  assert.deepEqual(
    visibleChatWatchers(watchers, "project-chat", null).map((item) => item.watcher_id),
    [],
  );
});

test("an unsettled Experiment stop exposes exact paused recovery before graceful waiting", () => {
  const node = experiment("experiment/stopping-paused");
  const paused = {
    ...loopTask(
      "stopping-paused-task",
      node.id,
      "episode-stopping-paused",
      "paused",
      "2026-08-06T03:00:00Z",
    ),
    can_resume: true,
    can_retry: true,
  };
  const experimentControl = control(
    {
      ready: false,
      reasons: ["A graceful stop is finishing the current loop turn."],
      episode_id: "episode-stopping-paused",
      invocations_used: 1,
      invocations_remaining: 2,
      health: "needs_action",
      recommendation: "resume",
      run_section: "actionable",
      live: true,
      can_start: false,
      can_stop: false,
      stop_pending: true,
      task_control: "resume",
      can_switch_provider: true,
    },
    {
      task_active: true,
      stop_requested: true,
      stop_settled: false,
      current_operation_id: paused.operation_id,
      current_status: "paused",
    },
  );

  const run = buildExperimentRun(node, experimentControl, [paused], []);
  assert.equal(run.health, "needs_action");
  assert.deepEqual(experimentRecommendation(run), {
    step: "resume",
    label: "Resume this episode, or switch provider",
  });
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

test("a succeeded legacy-attribution episode recommends a fresh episode directly", () => {
  const node = experiment("experiment/legacy-attribution");
  const succeeded = loopTask(
    "legacy-attribution-task",
    node.id,
    "episode-legacy-attribution",
    "succeeded",
    "2026-08-06T01:00:00Z",
  );
  const run = buildExperimentRun(
    node,
    control(
      {
        ready: true,
        reasons: [],
        episode_id: "episode-legacy-attribution",
        invocations_used: 1,
        invocations_remaining: 2,
      },
      {
        current_operation_id: succeeded.operation_id,
        current_status: "succeeded",
        session: {
          ...control().operational.session,
          diagnostic:
            "Automatic watcher wake stopped: an originating task predates durable human attribution, so RCP cannot prove who authorized the wake. Start a new Work turn or Experiment Run to continue.",
        },
      },
    ),
    [succeeded],
    [],
  );

  assert.equal(run.currentTask.status, "succeeded");
  assert.deepEqual(run.currentWatchers, []);
  assert.equal(run.health, "needs_action");
  assert.deepEqual(experimentRecommendation(run), {
    step: "start_episode",
    label: "Start a new episode",
  });
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
        health: "degraded",
        recommendation: "keep_loop",
        run_section: "running",
        live: true,
        can_start: false,
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
  assert.deepEqual(experimentRecommendation(run), {
    step: "keep_loop",
    label: "Keep loop running; check now if needed",
  });
});

test("Experiment recommendation copy follows the backend recommendation enum", () => {
  const base = {
    node: experiment("experiment/recommendation"),
    control: control({
      episode_id: "episode-recommendation",
      health: "agent_active",
      recommendation: "wait",
      run_section: "running",
    }),
    taskGroup: null,
    currentTask: null,
    watchers: [],
    watcherItems: [],
    currentWatchers: [],
    health: "agent_active",
  };
  assert.equal(experimentRecommendation(base).step, "wait");
  assert.equal(
    experimentRecommendation({
      ...base,
      control: control({
        episode_id: "episode-recommendation",
        recommendation: "stop_and_restart",
      }),
    }).step,
    "stop_and_restart",
  );
  assert.equal(
    experimentRecommendation({
      ...base,
      control: control({ recommendation: "start_episode" }),
      health: "human_stopped",
    }).step,
    "start_episode",
  );
  assert.equal(
    experimentRecommendation({
      ...base,
      control: control({
        ready: false,
        reasons: ["Blocker blk/required-input is open."],
        graph_reasons: ["Blocker blk/required-input is open."],
        episode_id: "episode-recommendation",
        recommendation: "resolve_requirements",
      }),
      health: "human_stopped",
    }).step,
    "resolve_requirements",
  );
});

test("only a generic watcher can be stopped on its own", () => {
  assert.equal(watcherIsIndividuallyStoppable({ continuation: { patch_kind: "work" } }), true);
  assert.equal(
    watcherIsIndividuallyStoppable({ continuation: { patch_kind: "experiment_loop" } }),
    false,
  );
  assert.equal(watcherIsIndividuallyStoppable({}), true);
});
