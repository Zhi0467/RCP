import { episode as baseEpisode } from "./campaigns.mjs";

const profile = { provider: "codex", model: "gpt-6-astra", reasoning: "high", run_on: "local" };
const ready = (provider) => ({
  provider,
  label: provider,
  installed: true,
  authenticated: true,
  models: [],
});

export const NODE_ID = "exp/shorter-thinking-steering-dflash-base";

export const taskProject = {
  id: "p",
  name: "Steering",
  run_on: "local",
  agent_profiles: { project_chat: profile, node_chat: profile, orchestrator: profile },
  provider_readiness: { local: { codex: ready("codex"), claude: ready("claude") } },
  repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
  project_truth_scope: ["repo"],
  state_repository: "repo",
  machines: [{ alias: "local", host: null }],
};

const at = (minute) => new Date(Date.UTC(2026, 10, 20, 15, minute)).toISOString();

const turn = (id, minute, question, answer) => [
  {
    message_id: `${id}-q`,
    operation_id: id,
    role: "user",
    text: question,
    timestamp: at(minute),
    mode: "work",
    trigger: "human",
    attachments: [],
  },
  {
    message_id: `${id}-a`,
    operation_id: id,
    role: "assistant",
    text: answer,
    timestamp: at(minute + 1),
    mode: "work",
    trigger: "human",
    attachments: [],
  },
];

export const taskHistory = [
  ...turn(
    "turn-1",
    0,
    "Should the controls reuse the original 22 starts?",
    "Yes to both.\n\n**The DFlash runs are new** on all **62 test starts**.",
  ),
  ...turn("turn-2", 10, "Tighten the plan.", "I tightened the plan."),
];

export const longTaskHistory = [
  ...taskHistory.slice(0, 2),
  ...Array.from({ length: 16 }, (_, index) =>
    turn(`later-${index}`, 20 + index * 2, `Question ${index}`, "A line of text. ".repeat(60)),
  ).flat(),
];

export function taskNode(overrides = {}) {
  return {
    id: NODE_ID,
    type: "experiment",
    title: "Testing frozen PEAR steering with a DFlash2 drafter",
    updated_rev: 3,
    invocation_ceiling: 30,
    design: "Compare PEAR with ordinary decoding against PEAR with DFlash2 speculative decoding.",
    completion_criteria: [
      "Accepted draft tokens per verification step reported for every arm",
      "Wall time per episode reported on all 62 test starts",
    ],
    ...overrides,
  };
}

export function taskSnapshot(graph) {
  return {
    ...taskProject,
    graph_target: { kind: "main" },
    graph: {
      edges: {},
      proposals: {},
      ambiguities: {},
      glossary: {},
      ontology: { types: [] },
      validation_messages: [],
      ...graph,
    },
    attention: {
      pending_proposal_ids: [],
      decisions_awaiting_choice_ids: [],
      open_blocker_ids: [],
      proposal_actions: {},
      stale_proposal_ids: [],
      refreshable_proposal_ids: [],
      stale_edge_ids: [],
      decision_prior_choices: {},
    },
  };
}

export function planQuestion(overrides = {}, card = {}) {
  return {
    question_id: "q1",
    owner_kind: "chat",
    owner_id: "chat",
    operation_id: "turn-1",
    capability: "work",
    question: "Start this Experiment?",
    choices: [],
    multiple: false,
    state: "pending",
    answer: null,
    chosen_choices: [],
    resolved_by: null,
    resolved_at: null,
    withdrawn_readonly: false,
    can_answer: true,
    agent_waiting: false,
    answer_continues: null,
    created_at: at(2),
    ...overrides,
    card: {
      kind: "experiment_start",
      action: {
        kind: "experiment_start",
        project_id: "p",
        node_id: NODE_ID,
        goal: "Run the three DFlash arms on all 62 test starts and both controls on the 40 new starts.",
        invocation_ceiling: 30,
      },
      pinned: {},
      pinned_sha256: "sha-1",
      expires_at: at(600),
      state: "pending",
      result: null,
      started_episode_id: null,
      current_episode_id: null,
      shelved_at: null,
      revised_at: null,
      revised_by: null,
      revised_operation_id: null,
      run_here_operation_id: null,
      ...card,
    },
  };
}

export function presentation(overrides = {}) {
  return {
    phase: "running",
    title: "Testing frozen PEAR steering with a DFlash2 drafter",
    label: "Running",
    state_line: "Running · turn 4 of 30",
    tone: "running",
    section: "running",
    needs_you: false,
    recommendation: "wait",
    actions: [
      {
        kind: "stop",
        episode_id: "episode/one",
        operation_id: null,
        report_episode_id: null,
        node_id: null,
      },
    ],
    ...overrides,
  };
}

export function dockRun(id, shown = {}, overrides = {}) {
  const display = presentation(shown);
  return {
    episode: {
      ...baseEpisode,
      project_id: "p",
      episode_id: id,
      mode: "experiment_loop",
      control_node_id: NODE_ID,
      tasks: [],
      health: display.needs_you ? "needs_action" : baseEpisode.health,
      created_at: new Date(Date.now() - 12 * 60_000).toISOString(),
      ...overrides.episode,
      presentation: display,
    },
    title: display.title,
    source: "chat",
    link: `#/projects/p/targets/main/episode/${encodeURIComponent(id)}`,
    questions: [],
    ...overrides.run,
  };
}

export const endedPresentation = (title) => ({
  phase: "ended",
  title,
  label: "Completed",
  state_line: "Completed · 9 turns",
  tone: "completed",
  section: "ended",
  needs_you: false,
  recommendation: "open_report",
  actions: [
    {
      kind: "open_report",
      episode_id: null,
      operation_id: null,
      report_episode_id: "episode/done",
      node_id: null,
    },
  ],
});

export function artifact(id, name, mediaType, view) {
  return {
    artifact_id: id,
    name,
    media_type: mediaType,
    view,
    supplier: "agent",
    worker_label: null,
    origin_operation_id: "turn-1",
    created_at: new Date(Date.now() - 60 * 60_000).toISOString(),
  };
}

/**
 * The backend a task card fixture talks to, held by the test. Each read
 * answers from `state`; taps and Syncs are recorded and answered by `onDecision`
 * and `onSync`.
 */
export async function serveTaskCards(page, state) {
  const log = { decisions: [], syncs: [], order: [], stops: [] };
  const json = (route, body, status = 200) => route.fulfill({ status, json: body });
  await page.route("**/api/**", (route) => json(route, { detail: "not found" }, 404));
  await page.route("**/api/resource-watermarks", (route) => json(route, {}));
  await page.route("**/api/projects/p/chats/chat/questions", (route) =>
    json(route, state.questions),
  );
  await page.route("**/api/projects/p/chats/chat/cards", (route) =>
    json(route, {
      proposal_cards: [],
      asked_cards: state.questions
        .filter((question) => question.state === "pending" && question.card.state === "pending")
        .map((question) => ({ ...question, project_name: "Steering" })),
      questions: [],
      episodes: state.episodes ?? [],
      next_expiry_at: null,
    }),
  );
  await page.route("**/api/agent-threads/chat/artifact-list", (route) =>
    json(route, state.artifacts ?? []),
  );
  await page.route(/\/api\/projects\/p(\?.*)?$/, (route) => {
    log.order.push("read");
    return json(route, taskSnapshot(state.graph));
  });
  await page.route("**/api/projects/p/sync", (route) => {
    const body = route.request().postDataJSON();
    log.syncs.push(body);
    log.order.push("sync");
    const answer = state.onSync(body);
    return json(route, answer.body, answer.status ?? 200);
  });
  await page.route("**/api/approval-cards/*", (route) => {
    const body = route.request().postDataJSON();
    log.decisions.push(body);
    log.order.push(`decision:${body.decision}`);
    const answer = state.onDecision(body);
    return json(route, answer.body, answer.status ?? 200);
  });
  await page.route("**/api/projects/p/episodes/*/stop", (route) => {
    log.stops.push(decodeURIComponent(new URL(route.request().url()).pathname));
    return json(route, { detail: "refused" }, 409);
  });
  return log;
}
