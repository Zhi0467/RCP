import { ResourceRuntime } from "./resourceRuntime";
import { createRoot } from "react-dom/client";
import { DetailDrawer } from "../../src/graph/DetailDrawer";
import { ExecutionView } from "../../src/graph/GraphViews";
import { buildGlossaryIndex } from "../../src/graph/glossary";
import type { Episode, GraphNode, GraphState, ProjectSnapshot } from "../../src/core/types";
import { episode as base } from "./campaigns.mjs";
import { RUN_STATE_CASES } from "./runStates.mjs";
import { withEpisodeAnswers, withExperimentControlAnswers } from "../taskAnswers.mjs";
import "../../src/styles.css";

/**
 * `?view=runs` draws Runs with one Experiment in each task-card state beside two
 * Auto-research runs. `?view=drawer` draws an Experiment node whose run paused
 * for its human; `&stop=1` leaves a Stop unsettled on it.
 */
const query = new URLSearchParams(location.search);
const profile = {
  provider: "codex",
  model: "",
  reasoning: "medium",
  runtime: "codex_app_server",
  run_on: "local",
  effective_model: "gpt-5.6",
  work_like_capable: true,
  permissions: {},
};
const project = {
  id: "project one",
  name: "Project",
  agent_profiles: { orchestrator: profile, node_chat: profile, project_chat: profile },
  provider_readiness: {
    local: {
      codex: {
        provider: "codex",
        label: "Codex",
        installed: true,
        authenticated: true,
        models: [],
      },
    },
  },
  repositories: [],
  project_truth_scope: [],
  run_on: "local",
  machines: [{ machine_id: "local" }],
} as unknown as ProjectSnapshot;

const experimentNode = (id: string, title: string, status = "running") =>
  ({
    id,
    type: "experiment",
    title,
    objective: "Measure whether the effect survives the held-out split.",
    design: "",
    expected_outcomes: [],
    interpretation_rules: [],
    completion_criteria: [],
    invocation_ceiling: 8,
    attempts: [],
    current_summary: "The sweep finished two of three seeds.",
    next_action: null,
    status,
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  }) as unknown as GraphNode;

const operational = {
  task_active: false,
  detached_work_active: false,
  watcher_degraded: false,
  watcher_completion_pending: false,
  episode_exited: false,
  episode_live: false,
  stop_requested: false,
  stop_settled: false,
  chat_id: "chat-1",
  current_operation_id: null,
  current_status: null,
  current_phase: null,
  current_status_message: null,
  current_last_activity_at: "2026-08-12T08:02:00Z",
  current_invocation: 3,
  session: {
    provider: "codex",
    model: "gpt-5.6",
    reasoning: "medium",
    run_on: "local",
    run_truth_scope: [],
    native_session_bound: true,
    diagnostic: null,
  },
};

function experimentRun(index: number, title: string, controlFields: object, episodeFields: object) {
  const nodeId = `exp/${index}`;
  const episode = {
    ...base,
    mode: "experiment_loop",
    control_node_id: nodeId,
    episode_id: `episode/exp-${index}`,
    created_at: `2026-08-12T0${index}:00:00Z`,
    tasks: [],
    task_control: null,
    can_stop: false,
    current_control_task_id: null,
    started_by: { kind: "human", human: base.authorized_by, auto_research_episode_id: null },
    checkout: { kind: "shared", available: true, machine_id: "local", repository_paths: [] },
    chain: [],
    ...episodeFields,
  };
  const control = withExperimentControlAnswers(
    {
      ready: true,
      reasons: [],
      graph_reasons: [],
      isolated_start_reasons: [],
      invocations_used: 3,
      invocation_ceiling: 8,
      invocations_remaining: 5,
      episode_id: episode.episode_id,
      episode,
      paused: false,
      active: false,
      governing_decisions: [],
      decision_drift: [],
      operational,
      ...controlFields,
    },
    title,
  );
  return { node: experimentNode(nodeId, title), control, episode: control.episode as Episode };
}

const noop = () => {};
const never = async () => {};

function Runs() {
  const runs = RUN_STATE_CASES.map((state, index) =>
    experimentRun(index + 1, `Held-out sweep, ${state.name}`, state.control, state.episode),
  );
  const autoResearch = [
    withEpisodeAnswers({
      ...base,
      episode_id: "episode/auto-live",
      run_section: "running",
      chain: [],
    }),
    withEpisodeAnswers({
      ...base,
      episode_id: "episode/auto-spent",
      created_at: "2026-08-12T07:00:00Z",
      health: "needs_action",
      ending: "exhausted",
      live: false,
      can_stop: false,
      can_continue: true,
      task_control: null,
      recommendation: "reauthorize",
      blocked_reason: "reauthorize",
      run_section: "actionable",
      chain: [],
    }),
  ] as unknown as Episode[];
  const graph = {
    nodes: Object.fromEntries(runs.map((run) => [run.node.id, run.node])),
  } as unknown as GraphState;
  return (
    <main className="app-main" style={{ padding: 16 }}>
      <ExecutionView
        {...({
          graph,
          project,
          episodes: [...runs.map((run) => run.episode), ...autoResearch],
          episodeMessages: {},
          episodeAction: null,
          tasks: [],
          watchers: [],
          experimentControl: Object.fromEntries(runs.map((run) => [run.node.id, run.control])),
          selectedExperimentId: query.get("open"),
          focusExperimentId: null,
          selectedAutoResearchEpisodeId: null,
          runBusy: false,
          stopBusyIds: new Set(),
          taskActionId: null,
          onInspectTask: noop,
          onLoadEpisodeMessages: never,
          onStopEpisode: never,
          onMergeEpisode: never,
          onContinueEpisode: never,
          onSendEpisodeMessage: never,
          onOperateEpisodeTask: never,
          onSwitchEpisodeProvider: noop,
          onArchiveEpisode: never,
          onSelectExperiment: noop,
          onDetailFocused: noop,
          onRunExperiment: noop,
          onStopExperiment: noop,
          onStopExperimentWatcher: noop,
          onRecoverExperiment: noop,
          onSwitchExperimentProvider: noop,
          onOpenExperimentEntry: noop,
          episodeReportHref: () => "#",
        } as unknown as Parameters<typeof ExecutionView>[0])}
      />
    </main>
  );
}

function Drawer() {
  const { node, control } = experimentRun(
    1,
    "Held-out sweep",
    {
      health: "needs_action",
      run_section: "actionable",
      recommendation: "start_episode",
      paused: true,
      stop_pending: query.has("stop"),
      reasons: query.has("stop") ? ["A graceful stop is finishing the current loop turn."] : [],
      can_start: !query.has("stop"),
    },
    {
      health: "needs_action",
      ending: "human_pause",
      live: false,
      can_continue: true,
      budget: { ...base.budget, invocations_remaining: 5 },
    },
  );
  const resumed = (window as unknown as { resumed: unknown[] }).resumed;
  return (
    <DetailDrawer
      {...({
        node,
        allNodes: { [node.id]: node },
        edges: [],
        glossaryIndex: buildGlossaryIndex({}),
        beliefTransitions: [],
        validationMessages: [],
        ontology: { types: [], fields: [], relations: [] },
        detailSlot: "original",
        project,
        experimentControl: control,
        onResumeExperiment: (episodeId: string, turns: number) => {
          resumed.push([episodeId, turns]);
          return new Promise<void>((resolve) => Object.assign(window, { finishResume: resolve }));
        },
        onRunExperiment: noop,
        onClose: noop,
        onDock: noop,
        onBeginEdit: noop,
        onStanding: noop,
        onStage: noop,
        onOpenChat: noop,
        onOpenRelatedNode: noop,
        onExploreRelations: noop,
        onSelectNode: noop,
      } as unknown as Parameters<typeof DetailDrawer>[0])}
    />
  );
}

Object.assign(window, { resumed: [] });
createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>{query.get("view") === "drawer" ? <Drawer /> : <Runs />}</ResourceRuntime>,
);
