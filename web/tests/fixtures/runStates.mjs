/**
 * One Experiment run in each state a task card shows, as the control and the
 * episode would answer it, with the phase and tone every surface must render.
 */
export const RUN_STATE_CASES = [
  {
    name: "running",
    control: {
      health: "agent_active",
      run_section: "running",
      recommendation: "wait",
      live: true,
      can_stop: true,
    },
    episode: { health: "active", ending: null, live: true, can_stop: true },
    shows: { phase: "running", tone: "running" },
  },
  {
    name: "waiting on watchers",
    control: {
      health: "waiting_on_watchers",
      run_section: "running",
      recommendation: "wait",
      live: true,
      can_stop: true,
    },
    episode: { health: "active", ending: null, live: true, can_stop: true },
    shows: { phase: "running", tone: "waiting" },
  },
  {
    name: "paused at limit",
    control: {
      health: "paused_at_limit",
      run_section: "actionable",
      recommendation: "start_episode",
    },
    episode: { health: "needs_action", ending: "exhausted", live: false, can_continue: true },
    shows: { phase: "needs_you", tone: "actionable" },
  },
  {
    name: "stopped",
    control: {
      health: "human_stopped",
      run_section: "actionable",
      recommendation: "start_episode",
    },
    episode: { health: "stopped", ending: "stopped", live: false },
    shows: { phase: "ended", tone: "stopped" },
  },
  {
    name: "completed",
    control: { health: "completed", run_section: "completed", recommendation: "none" },
    episode: { health: "completed", ending: "completed", live: false },
    shows: { phase: "ended", tone: "completed" },
  },
  {
    name: "closed node after Stop",
    control: {
      health: "completed",
      run_section: "completed",
      recommendation: "none",
      node_closed: true,
      can_start: false,
    },
    episode: { health: "stopped", ending: "stopped", live: false },
    shows: { phase: "ended", tone: "completed" },
  },
];
