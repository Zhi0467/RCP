/**
 * Fill in the lifecycle answers the server derives from a task's status.
 *
 * Fixtures stand in for the projection, so they carry what it would have decided
 * rather than leaving the browser to work it out from `status` — which it can no
 * longer do, because the type is opaque.
 */
const LABELS = {
  queued: "Queued",
  running: "Running in the background",
  pausing: "Pausing",
  paused: "Paused at checkpoint",
  interrupted: "Interrupted",
};

export function withTaskAnswers(task) {
  const status = task.status;
  // Derived last: the answers always follow the status this fixture ended up with,
  // so a spread that changes the status cannot leave a stale answer behind.
  return {
    ...task,
    active: ["queued", "running", "pausing"].includes(status),
    queued: status === "queued",
    pausing: status === "pausing",
    awaiting_human: ["paused", "failed", "interrupted"].includes(status),
    paused: status === "paused",
    failed: status === "failed",
    settled: status === "succeeded",
    finished: ["succeeded", "failed", "interrupted"].includes(status),
    status_label:
      status === "succeeded"
        ? task.applied_revision
          ? `Completed at revision ${task.applied_revision}`
          : "Completed"
        : (LABELS[status] ?? "Failed"),
  };
}

/** The same answers for the loop turn the control projection reports. */
export function withTurnAnswers(operational) {
  const status = operational.current_status;
  return {
    ...operational,
    current_queued: status === "queued",
    current_active: ["queued", "running", "pausing"].includes(status),
    current_awaiting_human: ["paused", "failed", "interrupted"].includes(status),
  };
}

/**
 * Fill the Experiment Runs answers a backend control fixture must publish. The
 * presentation follows the answers the fixture ended up with, and the embedded
 * episode carries the same value, as the server sends it.
 */
export function withExperimentControlAnswers(control, title = "Experiment") {
  const answered = {
    health: "needs_action",
    recommendation: "start_episode",
    run_section: "actionable",
    live: false,
    can_start: true,
    can_stop: false,
    stop_pending: false,
    task_control: null,
    can_switch_provider: false,
    can_open_report: false,
    report_episode_id: null,
    report_is_current: false,
    node_closed: false,
    ...control,
  };
  const presentation =
    control.presentation === undefined
      ? experimentPresentation(answered, title)
      : control.presentation;
  return {
    ...answered,
    presentation,
    episode:
      answered.episode && presentation ? { ...answered.episode, presentation } : answered.episode,
  };
}

const RUN_DISPLAY = {
  starting: ["Starting", "running"],
  active: ["Running", "running"],
  recovering: ["Recovering", "waiting"],
  waiting_on_watchers: ["Waiting on watchers", "waiting"],
  completion_pending: ["Completion pending delivery", "waiting"],
  degraded: ["Watcher degraded", "degraded"],
  stopping: ["Stopping gracefully", "stopping"],
  wrapping_up: ["Wrapping up", "running"],
  failed: ["Failed", "degraded"],
  stopped: ["Stopped", "stopped"],
  paused_at_limit: ["Paused at invocation limit", "actionable"],
  needs_action: ["Needs action", "actionable"],
  completed: ["Completed", "completed"],
};
const EXPERIMENT_RUN_STATES = { agent_active: "active", human_stopped: "stopped" };

/** The label and tone the server publishes for an Experiment health. */
export function experimentRunDisplay(health) {
  const [label, tone] = RUN_DISPLAY[EXPERIMENT_RUN_STATES[health] ?? health];
  return { label, tone };
}

/** The server's `present_run`, for fixtures that stand in for its projection. */
export function presentRun({
  state,
  title,
  ended,
  needsYou,
  recommendation,
  used,
  ceiling,
  actions,
}) {
  const [label, tone] = RUN_DISPLAY[state];
  const section = ended ? "ended" : "running";
  const turns = ended ? `${used} turn${used === 1 ? "" : "s"}` : `turn ${used} of ${ceiling}`;
  return {
    phase: needsYou ? "needs_you" : section,
    title,
    label,
    state_line: used ? `${label} · ${turns}` : label,
    tone,
    section,
    needs_you: needsYou,
    recommendation,
    actions,
  };
}

/** The server's `run_actions`: each offered control with the record it acts on. */
export function runActions({
  episodeId,
  canStop,
  taskControl,
  controlTaskId,
  canContinue,
  reportEpisodeId,
  startNodeId = null,
}) {
  const action = (kind, fields) => ({
    kind,
    episode_id: null,
    operation_id: null,
    report_episode_id: null,
    node_id: null,
    ...fields,
  });
  const actions = [];
  if (taskControl && controlTaskId)
    actions.push(action(taskControl, { episode_id: episodeId, operation_id: controlTaskId }));
  if (canContinue) actions.push(action("continue", { episode_id: episodeId }));
  if (startNodeId) actions.push(action("start", { node_id: startNodeId }));
  if (canStop) actions.push(action("stop", { episode_id: episodeId }));
  if (reportEpisodeId) actions.push(action("open_report", { report_episode_id: reportEpisodeId }));
  return actions;
}

const ENDED_HEALTH = new Set(["completed", "failed", "stopped"]);

/** Fill the presentation the generic episode owner publishes from the episode's own answers. */
export function withEpisodeAnswers(episode, title = "Auto-research") {
  const state =
    episode.health === "needs_action" && episode.ending === "exhausted"
      ? "paused_at_limit"
      : episode.health;
  return {
    ...episode,
    presentation: presentRun({
      state,
      title,
      ended: state !== "wrapping_up" && (Boolean(episode.ending) || ENDED_HEALTH.has(state)),
      needsYou: episode.health === "needs_action" || episode.blocked_reason === "question",
      recommendation: episode.recommendation,
      used: episode.budget?.invocations_used ?? 0,
      ceiling: episode.budget?.invocation_ceiling ?? 0,
      actions: runActions({
        episodeId: episode.episode_id,
        canStop: Boolean(episode.can_stop),
        taskControl: episode.task_control,
        controlTaskId: episode.current_control_task_id,
        canContinue: Boolean(episode.can_continue),
        reportEpisodeId:
          episode.report && episode.wrapup_state === "ready" ? episode.episode_id : null,
      }),
    }),
  };
}

function experimentPresentation(control, title) {
  if (!control.episode_id) return null;
  const state = EXPERIMENT_RUN_STATES[control.health] ?? control.health;
  const episode = control.episode;
  return presentRun({
    state,
    title,
    ended:
      control.run_section === "completed" ||
      control.health === "human_stopped" ||
      episode?.presentation?.section === "ended",
    needsYou: control.run_section === "actionable" && control.health !== "human_stopped",
    recommendation: control.recommendation,
    used: control.invocations_used ?? 0,
    ceiling: control.invocation_ceiling ?? 0,
    actions: runActions({
      episodeId: control.episode_id,
      canStop: control.can_stop,
      taskControl: control.task_control,
      controlTaskId:
        episode?.current_control_task_id ?? control.operational?.current_operation_id ?? null,
      canContinue: Boolean(episode?.can_continue),
      reportEpisodeId: control.report_episode_id,
      startNodeId: control.ready ? (episode?.control_node_id ?? "experiment") : null,
    }),
  });
}
