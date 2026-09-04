import { useState } from "react";
import { createRoot } from "react-dom/client";

import { experimentBoardHref, experimentBoardRouteToken } from "../../src/experimentBoard";
import { ExecutionView } from "../../src/views/GraphViews";

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
  can_stop: false,
  can_reauthorize: false,
  can_message: false,
  live: false,
  health: "needs_action",
  recommendation: "review",
  task_control: null,
  run_section: "needs_action",
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
  run_section: "needs_action",
  live: false,
  can_start: false,
  can_stop: false,
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
  can_reauthorize: false,
  can_message: false,
  live: false,
  health: "needs_action",
  recommendation: "review",
  task_control: null,
  run_section: "needs_action",
};

function Fixture() {
  const [selectedExperimentId, setSelectedExperimentId] = useState<string | null>(experimentId);
  return (
    <ExecutionView
      graph={{
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
      }}
      episodes={[parentEpisode] as never}
      episodeMessages={{}}
      episodeAction={null}
      tasks={[]}
      watchers={[]}
      experimentControl={{}}
      experimentEntries={[entry] as never}
      exactExperimentRoute={
        {
          experiment_id: experimentId,
          episode_id: episode.episode_id,
          graph_target: episode.graph_target,
          parent_episode_id: parentEpisodeId,
        } as never
      }
      exactExperimentEntry={entry as never}
      selectedExperimentId={selectedExperimentId}
      focusExperimentId={null}
      selectedAutoResearchEpisodeId={parentEpisodeId}
      runBusy={false}
      stopBusyId={null}
      watcherCheckBusyId={null}
      taskActionId={null}
      selectedExperimentConversation={
        selectedExperimentId ? <div>Selected child transcript</div> : null
      }
      onInspectTask={() => undefined}
      onLoadEpisodeMessages={() => Promise.resolve()}
      onStopEpisode={() => Promise.resolve()}
      onMergeEpisode={() => Promise.resolve()}
      onReauthorizeEpisode={() => Promise.resolve()}
      onSendEpisodeMessage={() => Promise.resolve()}
      onOperateEpisodeTask={() => Promise.resolve()}
      onSelectExperiment={setSelectedExperimentId}
      onOpenExperimentEntry={(nextEntry) => {
        window.location.hash = experimentBoardHref(
          nextEntry.project_id,
          experimentBoardRouteToken(nextEntry),
        ).slice(1);
      }}
      onDetailFocused={() => undefined}
      onOpenHistory={() => undefined}
      onRunExperiment={() => undefined}
      onStopExperiment={() => undefined}
      onCheckExperimentWatcher={() => undefined}
      onRecoverExperiment={() => undefined}
      onSwitchExperimentProvider={() => undefined}
      episodeReportHref={() => "#"}
    />
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
