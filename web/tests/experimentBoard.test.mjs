import assert from "node:assert/strict";
import { withExperimentControlAnswers, withTaskAnswers, withTurnAnswers } from "./taskAnswers.mjs";
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
const {
  experimentBoardHref,
  experimentBoardRouteToken,
  experimentIndexEntryForRoute,
  experimentStopPath,
  experimentTerminalLabel,
  parseProjectHash,
  projectExperimentExecution,
  projectHashAfterViewChange,
  projectRunsNeedsExperimentIndex,
} = await server.ssrLoadModule("/src/experimentBoard.ts");
const { ExperimentBoard } = await server.ssrLoadModule("/src/components/ExperimentBoard.tsx");
const { NodeChat } = await server.ssrLoadModule("/src/components/NodeChat.tsx");
const { ExecutionView, focusRunDetail } = await server.ssrLoadModule("/src/views/GraphViews.tsx");

after(() => server.close());

function node(id, status = "planned", updatedRev = 1) {
  return {
    id,
    type: "experiment",
    title: `Experiment ${id}`,
    extension_fields: {},
    standing: "asserted",
    created_rev: 1,
    updated_rev: updatedRev,
    source_refs: [],
    status,
    attempts: [],
  };
}

function control(fields = {}, operationalFields = {}) {
  return withExperimentControlAnswers({
    ready: true,
    reasons: [],
    invocations_used: 1,
    invocation_ceiling: 3,
    invocations_remaining: 2,
    episode_id: "episode-1",
    episode: null,
    paused: false,
    active: false,
    governing_decisions: [],
    decision_drift: [],
    operational: withTurnAnswers({
      task_active: false,
      detached_work_active: false,
      watcher_degraded: false,
      watcher_completion_pending: false,
      episode_exited: false,
      episode_live: false,
      stop_requested: false,
      stop_settled: false,
      chat_id: "chat-1",
      current_operation_id: "operation-1",
      current_status: null,
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
      ...operationalFields,
    }),
    ...fields,
  });
}

function entry(id, nodeStatus, controlState, projectName = "Project") {
  return {
    project_id: `project-${projectName}`,
    project_name: projectName,
    project_reachable: true,
    graph_target: { kind: "main" },
    graph_head: null,
    parent_episode_id: null,
    parent_watching: false,
    node: node(id, nodeStatus),
    control: controlState,
    episode: controlState.episode,
  };
}

function episode(fields = {}) {
  return {
    episode_id: "episode-1",
    project_id: "project",
    mode: "experiment_loop",
    control_node_id: "wrapping",
    graph_target: { kind: "main" },
    graph_base_head: null,
    graph_branch: null,
    root_operation_id: "operation-1",
    current_operation_id: null,
    current_orchestrator_task_id: null,
    current_control_task_id: null,
    recovery: null,
    status: "wrapping_up",
    starting_instruction: null,
    budget: {
      invocation_ceiling: 3,
      invocations_used: 1,
      invocations_remaining: 2,
      observed_input_tokens: 0,
      observed_generated_tokens: 0,
    },
    authorized_by: null,
    stop_requested_at: null,
    ending: "completed",
    ending_diagnostic: null,
    wrapup_state: "running",
    wrapup_error: null,
    created_at: "2026-08-06T01:00:00Z",
    updated_at: "2026-08-06T02:00:00Z",
    ended_at: null,
    tasks: [],
    report: null,
    can_stop: false,
    can_reauthorize: false,
    can_message: false,
    live: true,
    health: "wrapping_up",
    recommendation: "wait",
    task_control: null,
    run_section: "needs_action",
    ...fields,
  };
}

test("the board shows the shared report wrap-up as in-progress", () => {
  const wrapping = episode();
  const entryValue = entry(
    "wrapping",
    "active",
    control({
      episode: wrapping,
      health: "wrapping_up",
      recommendation: "wait",
      run_section: "running",
    }),
  );
  const html = renderToStaticMarkup(
    React.createElement(ExperimentBoard, { entries: [entryValue], onOpen() {} }),
  );

  assert.match(html, /Wrapping up visualization and report/);
});

test("a final report error never becomes the board's episode health", () => {
  const reportFailed = episode({
    status: "completed",
    ending: "completed",
    wrapup_state: "failed",
    wrapup_error: "The visual report could not be generated.",
  });
  const entryValue = entry(
    "wrapping",
    "active",
    control({
      episode: reportFailed,
      health: "completed",
      recommendation: "none",
      run_section: "completed",
    }),
  );
  const html = renderToStaticMarkup(
    React.createElement(ExperimentBoard, { entries: [entryValue], onOpen() {} }),
  );

  assert.match(html, /Completed/);
  assert.doesNotMatch(html, /Report error/);
});

test("finished outcome labels stay distinct", () => {
  assert.equal(experimentTerminalLabel("completed"), "Succeeded");
  assert.equal(experimentTerminalLabel("abandoned"), "Abandoned");
  assert.equal(experimentTerminalLabel("superseded"), "Superseded");
});

test("experiment links round-trip through the project hash parser", () => {
  const href = experimentBoardHref("remote project/one", "experiment/alpha beta");
  assert.equal(
    href,
    "#/projects/remote%20project%2Fone?view=runs&experiment=experiment%2Falpha%20beta",
  );
  assert.deepEqual(parseProjectHash(href), {
    projectId: "remote project/one",
    view: "execution",
    projectViewSpecified: true,
    experimentId: "experiment/alpha beta",
    experimentRoute: null,
    autoResearchEpisodeId: null,
  });
  assert.deepEqual(parseProjectHash("#/projects/remote%20project%2Fone"), {
    projectId: "remote project/one",
    view: "overview",
    projectViewSpecified: false,
    experimentId: null,
    experimentRoute: null,
    autoResearchEpisodeId: null,
  });
  assert.deepEqual(parseProjectHash("#/projects/new"), {
    projectId: null,
    view: "overview",
    projectViewSpecified: false,
    experimentId: null,
    experimentRoute: null,
    autoResearchEpisodeId: null,
  });
  assert.equal(projectHashAfterViewChange(href, "overview"), "#/projects/remote%20project%2Fone");
  assert.equal(projectHashAfterViewChange(href, "execution"), null);
  assert.equal(projectHashAfterViewChange("#/projects/project-one", "attention"), null);
});

test("branch Experiment links carry the exact child episode and target identity", () => {
  const branchId = "parent/episode";
  const childEpisode = episode({
    episode_id: "child/episode",
    control_node_id: "experiment/branch-only",
    status: "running",
    graph_target: { kind: "branch", branch_id: branchId },
  });
  const indexed = {
    ...entry(
      "experiment/branch-only",
      "active",
      control({ episode_id: childEpisode.episode_id, episode: childEpisode }),
    ),
    project_id: "project/one",
    graph_target: { kind: "branch", branch_id: branchId },
    graph_head: {
      target: { kind: "branch", branch_id: branchId },
      revision: 9,
      transition_id: "transition-nine",
    },
    parent_episode_id: branchId,
    episode: childEpisode,
  };

  const href = experimentBoardHref(indexed.project_id, experimentBoardRouteToken(indexed));
  assert.equal(
    href,
    "#/projects/project%2Fone?view=runs&experiment=experiment%2Fbranch-only&episode=child%2Fepisode&target=branch&branch=parent%2Fepisode&parent=parent%2Fepisode",
  );
  const parsed = parseProjectHash(href);
  assert.deepEqual(parsed.experimentRoute, {
    experiment_id: "experiment/branch-only",
    episode_id: "child/episode",
    graph_target: { kind: "branch", branch_id: branchId },
    parent_episode_id: branchId,
  });
  assert.equal(
    experimentIndexEntryForRoute([indexed], indexed.project_id, parsed.experimentRoute),
    indexed,
  );
  assert.equal(
    experimentStopPath("/api/projects/project%2Fone", indexed.node.id, childEpisode.episode_id),
    "/api/projects/project%2Fone/experiments/experiment%2Fbranch-only/stop?episode_id=child%2Fepisode",
  );
});

test("project Runs polls the Experiment index before a branch child is selected", () => {
  assert.equal(projectRunsNeedsExperimentIndex("project-one", "execution"), true);
  assert.equal(projectRunsNeedsExperimentIndex("project-one", "overview"), false);
  assert.equal(projectRunsNeedsExperimentIndex(null, "execution"), false);
});

test("project Runs shows a dispatched child as a nested turn and its own run card", () => {
  const parentEpisodeId = "auto-research-parent";
  const childTask = withTaskAnswers({
    operation_id: "child-agent-turn",
    project_id: "project-one",
    kind: "node_chat",
    status: "running",
    request: {
      provider: "codex",
      model: null,
      reasoning: "medium",
      run_on: "local",
      patch_kind: "experiment_loop",
      control_node_id: "experiment/branch-child",
      control_episode_id: "child-experiment-episode",
      control_invocation: 1,
    },
    created_at: "2026-08-06T01:00:00Z",
    updated_at: "2026-08-06T01:01:00Z",
    status_message: "Agent task is running.",
    error: null,
    attempt: 1,
    estimate_seconds: 60,
    estimate_samples: 1,
    phase: "provider",
    elapsed_seconds: 1,
    progress: 0,
    can_pause: true,
    can_resume: false,
    can_retry: false,
    role: "orchestrator",
    depth: 0,
  });
  const childEpisode = episode({
    episode_id: "child-experiment-episode",
    project_id: "project-one",
    control_node_id: "experiment/branch-child",
    graph_target: { kind: "branch", branch_id: parentEpisodeId },
    root_operation_id: childTask.operation_id,
    current_operation_id: childTask.operation_id,
    current_control_task_id: childTask.operation_id,
    status: "running",
    ending: null,
    wrapup_state: "not_started",
    budget: {
      invocation_ceiling: 5,
      invocations_used: 1,
      invocations_remaining: 4,
      observed_input_tokens: 0,
      observed_generated_tokens: 0,
    },
    can_stop: true,
    live: true,
    health: "active",
    recommendation: "wait",
    run_section: "needs_action",
    tasks: [childTask],
  });
  const childControl = control(
    {
      episode_id: childEpisode.episode_id,
      episode: childEpisode,
      active: true,
      health: "agent_active",
      recommendation: "wait",
      run_section: "running",
      live: true,
      can_start: false,
      can_stop: true,
    },
    {
      task_active: true,
      current_operation_id: childTask.operation_id,
      current_status: "running",
      current_status_message: "Agent task is running.",
    },
  );
  const indexed = {
    ...entry("experiment/branch-child", "running", childControl),
    project_id: "project-one",
    graph_target: { kind: "branch", branch_id: parentEpisodeId },
    graph_head: {
      target: { kind: "branch", branch_id: parentEpisodeId },
      revision: 4,
      transition_id: "branch-four",
    },
    parent_episode_id: parentEpisodeId,
    parent_watching: true,
    node: {
      ...node("experiment/branch-child", "running"),
      title: "Reproduce the baseline",
    },
    episode: childEpisode,
  };
  const parentBefore = {
    ...childTask,
    operation_id: "parent-before-child",
    episode_id: parentEpisodeId,
    created_at: "2026-08-06T00:00:00Z",
    status_message: "Parent turn before child.",
    role: "orchestrator",
    depth: 0,
  };
  const parentAfter = {
    ...childTask,
    operation_id: "parent-after-child",
    episode_id: parentEpisodeId,
    created_at: "2026-08-06T02:00:00Z",
    status_message: "Parent wake after child.",
    role: "wake",
    depth: 0,
  };
  const parentEpisode = episode({
    episode_id: parentEpisodeId,
    project_id: "project-one",
    mode: "auto_research",
    control_node_id: null,
    root_operation_id: "orchestrator-turn",
    graph_target: { kind: "branch", branch_id: parentEpisodeId },
    status: "running",
    ending: null,
    wrapup_state: "not_started",
    can_stop: true,
    can_message: true,
    live: true,
    health: "active",
    recommendation: "continue",
    run_section: "needs_action",
    tasks: [parentAfter, parentBefore],
  });
  const html = renderToStaticMarkup(
    React.createElement(ExecutionView, {
      graph: {
        revision: 3,
        nodes: {},
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
      episodes: [parentEpisode],
      episodeMessages: {},
      episodeAction: null,
      tasks: [],
      watchers: [
        {
          watcher_id: "parent-completion-watch",
          origin_task_kind: "auto_research",
          episode_id: parentEpisodeId,
          graph_target: { kind: "branch", branch_id: parentEpisodeId },
          status: "active",
          condition: {
            node_id: indexed.node.id,
            status_in: ["abandoned", "completed"],
          },
          continuation: {
            patch_kind: "work",
            control_node_id: null,
            control_episode_id: null,
          },
        },
      ],
      experimentControl: {},
      experimentEntries: [indexed],
      selectedExperimentId: indexed.node.id,
      focusExperimentId: null,
      selectedAutoResearchEpisodeId: parentEpisodeId,
      runBusy: false,
      stopBusyId: null,
      watcherCheckBusyId: null,
      taskActionId: null,
      onInspectTask() {},
      async onLoadEpisodeMessages() {},
      async onStopEpisode() {},
      async onMergeEpisode() {},
      async onReauthorizeEpisode() {},
      async onSendEpisodeMessage() {},
      async onOperateEpisodeTask() {},
      onSelectExperiment() {},
      onOpenExperimentEntry() {},
      onDetailFocused() {},
      onOpenHistory() {},
      onRunExperiment() {},
      onStopExperiment() {},
      onCheckExperimentWatcher() {},
      onRecoverExperiment() {},
      onSwitchExperimentProvider() {},
      episodeReportHref: () => "#",
    }),
  );

  assert.match(html, /Needs Action<\/h2><span>2<\/span>/);
  assert.match(html, /campaign-task depth-1/);
  assert.match(html, /campaign-task-role experiment">Experiment/);
  const parentBeforeIndex = html.indexOf("Parent turn before child.");
  const nestedChildIndex = html.indexOf("Reproduce the baseline", parentBeforeIndex);
  const parentAfterIndex = html.indexOf("Parent wake after child.");
  assert.ok(parentBeforeIndex >= 0);
  assert.ok(parentBeforeIndex < nestedChildIndex);
  assert.ok(nestedChildIndex < parentAfterIndex);
  assert.match(html, /Turns<\/h3><span>3<\/span>/);
  assert.match(html, /campaign-task depth-1[\s\S]*?<strong>Reproduce the baseline<\/strong>/);
  assert.doesNotMatch(html, /campaign-task-copy"><strong>Reproduce the baseline<\/strong><span>/);
  assert.match(html, /campaign-run-title[\s\S]*?<span>Reproduce the baseline<\/span>/);
  assert.match(
    html,
    /href="#\/projects\/project-one\?view=runs&amp;experiment=experiment%2Fbranch-child&amp;episode=child-experiment-episode&amp;target=branch&amp;branch=auto-research-parent&amp;parent=auto-research-parent"/,
  );
  assert.match(html, /1 \/ 5 invocations/);
  assert.match(html, /Wait for the active Experiment turn/);
  assert.match(html, /<h3>Current turn<\/h3><span>1<\/span>/);
  assert.match(html, /campaign-task-role worker">Agent/);
  assert.match(html, /The owning Auto-research episode is watching this Experiment/);
  assert.match(html, /Stop loop/);
  assert.doesNotMatch(html, /Start episode/);
});

test("branch projection replaces colliding main state and filters control resources by target", () => {
  const branchId = "parent-episode";
  const childEpisode = episode({
    episode_id: "child-episode",
    control_node_id: "experiment/shared",
    status: "running",
    graph_target: { kind: "branch", branch_id: branchId },
    tasks: [],
  });
  const branchControl = control(
    { episode_id: childEpisode.episode_id, episode: childEpisode, active: true },
    { task_active: true, current_status: "running" },
  );
  const indexed = {
    ...entry("experiment/shared", "active", branchControl),
    project_id: "project-one",
    graph_target: { kind: "branch", branch_id: branchId },
    graph_head: {
      target: { kind: "branch", branch_id: branchId },
      revision: 8,
      transition_id: "branch-transition",
    },
    parent_episode_id: branchId,
    node: { ...node("experiment/shared", "active"), title: "Branch-modified title" },
    episode: childEpisode,
  };
  const route = parseProjectHash(
    experimentBoardHref(indexed.project_id, experimentBoardRouteToken(indexed)),
  ).experimentRoute;
  const mainTask = {
    operation_id: "main-task",
    graph_target: { kind: "main" },
    request: {
      patch_kind: "experiment_loop",
      control_node_id: indexed.node.id,
      control_episode_id: "main-episode",
    },
  };
  const branchTask = {
    operation_id: "branch-task",
    graph_target: indexed.graph_target,
    request: {
      patch_kind: "experiment_loop",
      control_node_id: indexed.node.id,
      control_episode_id: childEpisode.episode_id,
    },
  };
  const watcher = (watcherId, graphTarget, episodeId) => ({
    watcher_id: watcherId,
    graph_target: graphTarget,
    continuation: {
      patch_kind: "experiment_loop",
      control_node_id: indexed.node.id,
      control_episode_id: episodeId,
    },
  });
  const projection = projectExperimentExecution(
    [{ ...node(indexed.node.id, "active"), title: "Stale main title" }],
    [mainTask, branchTask],
    [
      watcher("main-watcher", { kind: "main" }, "main-episode"),
      watcher("branch-watcher", indexed.graph_target, childEpisode.episode_id),
    ],
    { [indexed.node.id]: control({ episode_id: "main-episode" }) },
    route,
    indexed,
  );

  assert.deepEqual(
    projection.nodes.map((item) => item.title),
    ["Branch-modified title"],
  );
  assert.deepEqual(
    projection.tasks.map((item) => item.operation_id),
    ["branch-task"],
  );
  assert.deepEqual(
    projection.watchers.map((item) => item.watcher_id),
    ["branch-watcher"],
  );
  assert.equal(projection.experimentControl[indexed.node.id], branchControl);
});

test("an explicit main route becomes history when the Experiment advances concurrently", () => {
  const experiment = node("experiment/main", "active");
  const previousEpisode = episode({
    episode_id: "episode-previous",
    control_node_id: experiment.id,
    graph_target: { kind: "main" },
  });
  const currentEpisode = episode({
    episode_id: "episode-current",
    control_node_id: experiment.id,
    graph_target: { kind: "main" },
  });
  const currentControl = control({
    episode_id: currentEpisode.episode_id,
    episode: currentEpisode,
  });
  const route = {
    experiment_id: experiment.id,
    episode_id: previousEpisode.episode_id,
    graph_target: { kind: "main" },
    parent_episode_id: null,
  };
  const projection = projectExperimentExecution(
    [experiment],
    [],
    [],
    { [experiment.id]: currentControl },
    route,
    null,
  );

  assert.deepEqual(projection.staleMainRoute, route);
  const html = renderToStaticMarkup(
    React.createElement(ExecutionView, {
      graph: {
        revision: 5,
        nodes: { [experiment.id]: experiment },
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
      episodes: [currentEpisode, previousEpisode],
      episodeMessages: {},
      episodeAction: null,
      tasks: [],
      watchers: [],
      experimentControl: { [experiment.id]: currentControl },
      exactExperimentRoute: route,
      exactExperimentEntry: null,
      selectedExperimentId: experiment.id,
      focusExperimentId: experiment.id,
      runBusy: false,
      stopBusyId: null,
      watcherCheckBusyId: null,
      taskActionId: null,
      onInspectTask() {},
      onSelectExperiment() {},
      onDetailFocused() {},
      onOpenHistory() {},
      onRunExperiment() {},
      onStopExperiment() {},
      onCheckExperimentWatcher() {},
      onRecoverExperiment() {},
      onSwitchExperimentProvider() {},
      episodeReportHref: () => "#",
    }),
  );

  assert.match(html, /The requested Experiment episode is now in History\./);
  assert.match(html, />Open History</);
  assert.doesNotMatch(
    html,
    /campaign-run-detail|Expand Experiment loop episode|Start episode|Stop loop|episode-current/,
  );
});

test("a stale main index entry cannot duplicate the current Experiment card", () => {
  const experiment = node("experiment/main", "active");
  const previousEpisode = episode({
    episode_id: "episode-previous",
    project_id: "project-one",
    control_node_id: experiment.id,
    graph_target: { kind: "main" },
  });
  const currentEpisode = episode({
    episode_id: "episode-current",
    project_id: "project-one",
    control_node_id: experiment.id,
    graph_target: { kind: "main" },
  });
  const currentControl = control({
    episode_id: currentEpisode.episode_id,
    episode: currentEpisode,
  });
  const staleEntry = {
    ...entry(
      experiment.id,
      experiment.status,
      control({
        episode_id: previousEpisode.episode_id,
        episode: previousEpisode,
      }),
    ),
    project_id: "project-one",
    episode: previousEpisode,
  };
  const html = renderToStaticMarkup(
    React.createElement(ExecutionView, {
      graph: {
        revision: 5,
        nodes: { [experiment.id]: experiment },
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
      episodes: [currentEpisode, previousEpisode],
      episodeMessages: {},
      episodeAction: null,
      tasks: [],
      watchers: [],
      experimentControl: { [experiment.id]: currentControl },
      experimentEntries: [staleEntry],
      selectedExperimentId: experiment.id,
      focusExperimentId: null,
      runBusy: false,
      stopBusyId: null,
      watcherCheckBusyId: null,
      taskActionId: null,
      onInspectTask() {},
      onSelectExperiment() {},
      onDetailFocused() {},
      onOpenHistory() {},
      onRunExperiment() {},
      onStopExperiment() {},
      onCheckExperimentWatcher() {},
      onRecoverExperiment() {},
      onSwitchExperimentProvider() {},
      episodeReportHref: () => "#",
    }),
  );

  assert.match(html, /<h2>Needs Action<\/h2><span>1<\/span>/);
  assert.match(html, />episode-current<\/dd>/);
  assert.doesNotMatch(html, /episode-previous/);
});

test("an exact Auto-research route focuses and scrolls its accessible detail", () => {
  const calls = [];
  focusRunDetail({
    focus(options) {
      calls.push(["focus", options]);
    },
    scrollIntoView(options) {
      calls.push(["scroll", options]);
    },
  });

  assert.deepEqual(calls, [
    ["focus", { preventScroll: true }],
    ["scroll", { block: "center" }],
  ]);
});

test("branch-created Runs detail uses index truth and never offers a main Start action", () => {
  const branchId = "parent-episode";
  const childEpisode = episode({
    episode_id: "child-episode",
    control_node_id: "experiment/branch-created",
    status: "running",
    // A running episode has no ending fence yet; only fencing one enters wrap-up.
    ending: null,
    wrapup_state: "not_started",
    graph_target: { kind: "branch", branch_id: branchId },
    tasks: [],
    can_stop: true,
    live: true,
    health: "active",
    recommendation: "continue",
    run_section: "needs_action",
  });
  const indexed = {
    ...entry(
      "experiment/branch-created",
      "active",
      control(
        {
          episode_id: childEpisode.episode_id,
          episode: childEpisode,
          active: true,
          health: "agent_active",
          recommendation: "wait",
          run_section: "running",
          live: true,
          can_start: false,
          can_stop: true,
        },
        { task_active: true, current_status: "running" },
      ),
    ),
    project_id: "project-one",
    graph_target: { kind: "branch", branch_id: branchId },
    graph_head: {
      target: { kind: "branch", branch_id: branchId },
      revision: 6,
      transition_id: "branch-transition",
    },
    parent_episode_id: branchId,
    node: {
      ...node("experiment/branch-created", "active"),
      title: "Created only on the branch",
    },
    episode: childEpisode,
  };
  const route = parseProjectHash(
    experimentBoardHref(indexed.project_id, experimentBoardRouteToken(indexed)),
  ).experimentRoute;
  const html = renderToStaticMarkup(
    React.createElement(ExecutionView, {
      graph: {
        revision: 5,
        nodes: {},
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
      episodes: [childEpisode],
      episodeMessages: {},
      episodeAction: null,
      tasks: [],
      watchers: [],
      experimentControl: {},
      exactExperimentRoute: route,
      exactExperimentEntry: indexed,
      selectedExperimentId: indexed.node.id,
      focusExperimentId: null,
      runBusy: false,
      stopBusyId: null,
      watcherCheckBusyId: null,
      taskActionId: null,
      onSelectNode() {},
      onInspectTask() {},
      onDismissTask() {},
      onSelectExperiment() {},
      onDetailFocused() {},
      onRunExperiment() {},
      onStopExperiment() {},
      onCheckExperimentWatcher() {},
      onRecoverExperiment() {},
      onSwitchExperimentProvider() {},
      episodeReportHref: () => "#",
    }),
  );

  assert.match(html, /Created only on the branch/);
  assert.match(html, /Stop loop/);
  assert.doesNotMatch(html, /Start new episode|Start episode/);

  const stoppedEpisode = {
    ...childEpisode,
    status: "stopped",
    ending: "stopped",
    wrapup_state: "skipped",
    can_stop: false,
    live: false,
    health: "stopped",
    recommendation: "none",
    run_section: "completed",
  };
  const terminalIndexed = {
    ...indexed,
    control: control(
      {
        episode_id: stoppedEpisode.episode_id,
        episode: stoppedEpisode,
        active: false,
      },
      {
        task_active: false,
        stop_requested: true,
        stop_settled: true,
        current_operation_id: null,
        current_status: null,
      },
    ),
    episode: stoppedEpisode,
  };
  const terminalHtml = renderToStaticMarkup(
    React.createElement(ExecutionView, {
      graph: {
        revision: 5,
        nodes: {},
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
      episodes: [stoppedEpisode],
      episodeMessages: {},
      episodeAction: null,
      tasks: [],
      watchers: [],
      experimentControl: {},
      exactExperimentRoute: route,
      exactExperimentEntry: terminalIndexed,
      selectedExperimentId: terminalIndexed.node.id,
      focusExperimentId: null,
      runBusy: false,
      stopBusyId: null,
      watcherCheckBusyId: null,
      taskActionId: null,
      onSelectNode() {},
      onInspectTask() {},
      onDismissTask() {},
      onSelectExperiment() {},
      onDetailFocused() {},
      onRunExperiment() {},
      onStopExperiment() {},
      onCheckExperimentWatcher() {},
      onRecoverExperiment() {},
      onSwitchExperimentProvider() {},
      episodeReportHref: () => "#",
    }),
  );
  assert.match(terminalHtml, /Review the owning Auto-research episode/);
  assert.doesNotMatch(terminalHtml, /Start new episode|Start episode/);
});

test("branch-created and branch-modified Experiment transcripts are read-only", () => {
  const project = {
    id: "project-one",
    name: "Project One",
    agent_profiles: {
      node_chat: {
        provider: "codex",
        model: null,
        reasoning: null,
        run_on: "local",
        permissions: {},
      },
    },
    provider_readiness: {
      local: {
        codex: {
          provider: "codex",
          label: "Codex",
          installed: true,
          authenticated: true,
          binary_path: "/usr/bin/codex",
          path_state: "resolved",
          models: [],
        },
      },
    },
    repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
    project_truth_scope: ["repo"],
    state_repository: "repo",
    machines: [{ alias: "local", host: null }],
  };
  const branchNodes = [
    {
      ...node("experiment/branch-created", "active"),
      title: "Created only on the branch",
    },
    {
      ...node("experiment/shared", "active"),
      title: "Modified on the branch",
    },
  ];

  branchNodes.forEach((branchNode, index) => {
    const transcriptText = `Branch transcript ${index + 1} remains visible.`;
    const html = renderToStaticMarkup(
      React.createElement(NodeChat, {
        project,
        node: branchNode,
        nodes: { [branchNode.id]: branchNode },
        runScope: ["repo"],
        tasks: [],
        historyMessages: [
          {
            message_id: `message-${index}`,
            operation_id: `operation-${index}`,
            role: "assistant",
            text: transcriptText,
            timestamp: `2026-08-18T00:0${index}:00Z`,
            native_session_id: `session-${index}`,
            provider: "codex",
            model: null,
            reasoning: null,
            execution_machine: "local",
            applied_revision: null,
            mode: "work",
            graph_update: {
              status: "rejected",
              applied_revision: null,
              change_summary: [],
              proposal_ids: [],
              validation_messages: ["Branch graph update needs review."],
              correction_rounds: 0,
              repairable: true,
            },
            trigger: "human",
          },
        ],
        chatId: `branch-chat-${index}`,
        presentation: "workspace",
        fixedConversation: true,
        readOnly: true,
        onStartTask() {
          throw new Error("read-only branch transcript cannot start a task");
        },
        onInspectTask() {},
        onOpenInbox() {},
        onRepairGraphUpdate() {
          throw new Error("read-only branch transcript cannot repair a graph update");
        },
        onNewSession() {
          throw new Error("read-only branch transcript cannot start a session");
        },
        onClose() {},
        onResumeTask() {
          throw new Error("read-only branch transcript cannot resume a task");
        },
        onRetryTask() {
          throw new Error("read-only branch transcript cannot retry a task");
        },
      }),
    );

    assert.match(html, new RegExp(transcriptText.replace(".", "\\.")));
    assert.doesNotMatch(html, /chat-composer/);
    assert.doesNotMatch(html, /aria-label="Message"/);
    assert.doesNotMatch(html, /chat-send-button/);
    assert.doesNotMatch(html, /chat-mode-toggle/);
    assert.doesNotMatch(html, /aria-keyshortcuts/);
    assert.doesNotMatch(html, /type="file"/);
    assert.doesNotMatch(html, /chat-add-file/);
    assert.doesNotMatch(html, /chat-new-session|scope-trigger/);
    assert.match(html, /<button type="button" disabled="">[\s\S]*?Repair graph update<\/button>/);
  });
});

test("partial branch identity fails closed instead of selecting the same id on main", () => {
  assert.deepEqual(
    parseProjectHash(
      "#/projects/project-one?view=runs&experiment=experiment%2Fshared&episode=child&target=branch",
    ),
    {
      projectId: "project-one",
      view: "execution",
      projectViewSpecified: true,
      experimentId: null,
      experimentRoute: null,
      autoResearchEpisodeId: null,
    },
  );
});

test("the rendered board keeps finished work folded and unavailable work explicit", () => {
  const entries = [
    {
      ...entry("needs-human", "active", control({}, { current_status: "failed" })),
      project_reachable: false,
      node: {
        ...node("needs-human", "active"),
        next_action: "Choose the recovery path.",
        current_summary: "An older summary.",
      },
    },
    entry(
      "done",
      "superseded",
      control({ health: "completed", recommendation: "none", run_section: "completed" }),
    ),
  ];
  const html = renderToStaticMarkup(
    React.createElement(ExperimentBoard, { entries, onOpen: () => undefined }),
  );

  assert.match(html, /<h2 id="experiment-board-title">Experiments<\/h2>/);
  assert.match(html, /<details class="experiment-board-finished">/);
  assert.doesNotMatch(html, /<details[^>]+open/);
  assert.match(html, /Superseded/);
  assert.match(html, /Unavailable/);
  assert.match(html, /Choose the recovery path\./);
  assert.doesNotMatch(html, /An older summary\./);
  assert.doesNotMatch(html, />Run<|>Retry<|>Stop</);
});
