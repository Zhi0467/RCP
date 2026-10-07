import { useState } from "react";
import { createRoot } from "react-dom/client";

import {
  experimentBoardHref,
  experimentBoardRouteToken,
  experimentStopPath,
  type ExperimentRouteIdentity,
} from "../../src/experiments/experimentBoardModel";
import { ExecutionView } from "../../src/graph/GraphViews";

const coexisting = new URLSearchParams(window.location.search).has("coexisting");
const projectId = "project-one";
const experimentId = "experiment/branch-child";
const parentEpisodeId = "auto-research-parent";
const episode = {
  episode_id: "child-experiment-episode",
  project_id: projectId,
  mode: "experiment_loop",
  control_node_id: experimentId,
  graph_target: { kind: "branch", branch_id: parentEpisodeId },
  graph_base_head: null,
  graph_branch: null,
  root_operation_id: "child-turn",
  current_operation_id: null,
  current_orchestrator_task_id: null,
  current_control_task_id: null,
  recovery: null,
  status: "needs_action",
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
  ending: "human_pause",
  ending_diagnostic: null,
  wrapup_state: "legacy_unavailable",
  wrapup_error: null,
  created_at: "2026-09-03T12:00:00Z",
  updated_at: "2026-09-03T13:00:00Z",
  ended_at: "2026-09-03T13:00:00Z",
  tasks: [],
  report: null,
  can_stop: coexisting,
  can_continue: false,
  chain: [
    {
      episode_id: "child-experiment-episode",
      created_at: "2026-09-03T12:00:00Z",
      status: "needs_action",
      ending: "human_pause",
      invocation_ceiling: 3,
      invocations_used: 1,
      report: null,
    },
  ],
  can_message: false,
  live: false,
  health: "needs_action",
  recommendation: "review",
  task_control: null,
  run_section: "actionable",
};
const control = {
  ready: true,
  reasons: [],
  graph_reasons: [],
  invocations_used: 1,
  invocation_ceiling: 3,
  invocations_remaining: 2,
  episode_id: episode.episode_id,
  episode,
  paused: true,
  active: false,
  stop_pending: false,
  governing_decisions: [],
  decision_drift: [],
  health: "needs_action",
  recommendation: "review",
  run_section: "actionable",
  live: false,
  can_start: false,
  can_stop: coexisting,
  can_open_report: false,
  can_switch_provider: false,
  node_closed: false,
  task_control: null,
  report_episode_id: null,
  operational: {
    task_active: false,
    detached_work_active: false,
    watcher_degraded: false,
    watcher_completion_pending: false,
    episode_exited: true,
    episode_live: false,
    stop_requested: false,
    stop_settled: false,
    chat_id: "child-chat",
    current_operation_id: null,
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
  },
};
const entry = {
  project_id: projectId,
  project_name: "Project one",
  project_reachable: true,
  graph_target: episode.graph_target,
  graph_head: {
    target: episode.graph_target,
    revision: 4,
    transition_id: "branch-four",
  },
  parent_episode_id: parentEpisodeId,
  parent_watching: false,
  node: {
    id: experimentId,
    type: "experiment",
    title: "Reproduce the baseline",
    extension_fields: {},
    standing: "asserted",
    created_rev: 1,
    updated_rev: 2,
    source_refs: [],
    status: "running",
    objective: "Reproduce the baseline before comparing treatments.",
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
  },
  control,
  episode,
};
const parentEpisode = {
  episode_id: parentEpisodeId,
  project_id: projectId,
  mode: "auto_research",
  control_node_id: null,
  graph_target: { kind: "branch", branch_id: parentEpisodeId },
  graph_base_head: null,
  graph_branch: null,
  root_operation_id: "parent-turn",
  current_operation_id: null,
  current_orchestrator_task_id: null,
  current_control_task_id: null,
  recovery: null,
  status: "needs_action",
  starting_instruction: null,
  budget: {
    invocation_ceiling: 5,
    invocations_used: 1,
    invocations_remaining: 4,
    observed_input_tokens: 0,
    observed_generated_tokens: 0,
  },
  authorized_by: null,
  stop_requested_at: null,
  ending: "human_pause",
  ending_diagnostic: null,
  wrapup_state: "legacy_unavailable",
  wrapup_error: null,
  created_at: "2026-09-03T11:00:00Z",
  updated_at: "2026-09-03T13:00:00Z",
  ended_at: "2026-09-03T13:00:00Z",
  tasks: [],
  report: null,
  can_stop: false,
  can_continue: false,
  chain: [
    {
      episode_id: parentEpisodeId,
      created_at: "2026-09-03T11:00:00Z",
      status: "needs_action",
      ending: "human_pause",
      invocation_ceiling: 5,
      invocations_used: 1,
      report: null,
    },
  ],
  can_message: false,
  live: false,
  health: "needs_action",
  recommendation: "review",
  task_control: null,
  run_section: "actionable",
};

const mainEpisode = {
  ...episode,
  episode_id: "main-episode",
  created_at: "2026-09-03T11:00:00Z",
  graph_target: { kind: "main" },
  chain: [],
  can_stop: true,
};
const mainEntry = {
  ...entry,
  graph_target: mainEpisode.graph_target,
  graph_head: null,
  parent_episode_id: null,
  episode: mainEpisode,
  control: { ...control, episode_id: mainEpisode.episode_id, episode: mainEpisode },
};
const childRoute = {
  experiment_id: experimentId,
  episode_id: episode.episode_id,
  graph_target: episode.graph_target,
  parent_episode_id: parentEpisodeId,
} as ExperimentRouteIdentity;

function Fixture() {
  const [selectedExperimentId, setSelectedExperimentId] = useState<string | null>(experimentId);
  const [selectedRoute, setSelectedRoute] = useState<ExperimentRouteIdentity | null>(childRoute);
  const [stopBusyIds, setStopBusyIds] = useState(new Set<string>());
  return (
    <ExecutionView
      graphTarget={coexisting ? (episode.graph_target as never) : undefined}
      graph={{
        revision: 3,
        nodes: coexisting ? { [experimentId]: entry.node } : {},
        edges: {},
        proposals: {},
        ambiguities: {},
        glossary: {},
        validation_messages: [],
        belief_transitions: [],
        replay_status: "complete",
        replay_failure: null,
        ontology: { types: [], fields: [], relations: [] },
      }}
      episodes={(coexisting ? [mainEpisode] : [parentEpisode]) as never}
      episodeMessages={{}}
      episodeAction={null}
      tasks={[]}
      watchers={[]}
      experimentControl={coexisting ? ({ [experimentId]: entry.control } as never) : {}}
      experimentEntries={(coexisting ? [mainEntry, entry] : [entry]) as never}
      exactExperimentRoute={selectedRoute}
      exactExperimentEntry={selectedRoute?.graph_target.kind === "branch" ? (entry as never) : null}
      selectedExperimentId={selectedExperimentId}
      focusExperimentId={coexisting ? selectedExperimentId : null}
      selectedAutoResearchEpisodeId={coexisting ? null : parentEpisodeId}
      runBusy={false}
      stopBusyIds={stopBusyIds}
      watcherCheckBusyId={null}
      taskActionId={null}
      selectedExperimentConversation={
        selectedExperimentId ? (
          <div data-selected-episode={selectedRoute?.episode_id ?? mainEpisode.episode_id}>
            Selected child transcript
          </div>
        ) : null
      }
      onInspectTask={() => undefined}
      onLoadEpisodeMessages={() => Promise.resolve()}
      onStopEpisode={() => Promise.resolve()}
      onMergeEpisode={() => Promise.resolve()}
      onContinueEpisode={() => Promise.resolve()}
      onSendEpisodeMessage={() => Promise.resolve()}
      onOperateEpisodeTask={() => Promise.resolve()}
      onSelectExperiment={(nodeId, route) => {
        setSelectedExperimentId(nodeId);
        if (nodeId) setSelectedRoute(route ?? null);
      }}
      onOpenExperimentEntry={(nextEntry) => {
        window.location.hash = experimentBoardHref(
          nextEntry.project_id,
          experimentBoardRouteToken(nextEntry),
        ).slice(1);
      }}
      onDetailFocused={() => undefined}
      onOpenHistory={() => undefined}
      onRunExperiment={() => undefined}
      onStopExperiment={(nodeId, episodeId) => {
        setStopBusyIds((current) => new Set(current).add(episodeId));
        void fetch(experimentStopPath(`/api/projects/${projectId}`, nodeId, episodeId), {
          method: "POST",
        }).then(() => {
          setStopBusyIds((current) => {
            const pending = new Set(current);
            pending.delete(episodeId);
            return pending;
          });
        });
      }}
      onCheckExperimentWatcher={() => undefined}
      onRecoverExperiment={() => undefined}
      onSwitchExperimentProvider={() => undefined}
      episodeReportHref={() => "#"}
    />
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
