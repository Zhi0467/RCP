import { chromium } from "playwright";
import { createServer } from "vite";

export const now = Date.now();
export const ago = (minutes) => new Date(now - minutes * 60_000).toISOString();

export const threads = [
  {
    thread_id: "space",
    kind: "space",
    project_id: null,
    project_name: null,
    agent_name: "Pip",
    unread: false,
    last_activity_at: ago(0),
    live_state: "idle",
    browser_requested: true,
  },
  {
    thread_id: "moss",
    kind: "project",
    project_id: "p1",
    project_name: "Sparse attention",
    agent_name: "Moss",
    unread: true,
    /** The turn finishes after its messages are stamped. */
    last_finished_at: ago(7),
    last_activity_at: ago(12),
    live_state: "needs_you",
    browser_requested: true,
  },
  {
    thread_id: "biscuit",
    kind: "project",
    project_id: "p2",
    project_name: "Tokenizer ablations",
    agent_name: "Biscuit",
    unread: false,
    last_activity_at: ago(1),
    live_state: "running",
    browser_requested: true,
  },
  {
    thread_id: "juniper",
    kind: "project",
    project_id: "p3",
    project_name: "Long-context eval",
    agent_name: "Juniper",
    unread: false,
    last_activity_at: ago(60 * 50),
    live_state: "idle",
    browser_requested: true,
  },
  {
    thread_id: "gone",
    kind: "project",
    project_id: "p4",
    project_name: "Left project",
    agent_name: "Orlando",
    unread: false,
    last_activity_at: null,
    live_state: "closed",
    browser_requested: true,
  },
];

/** What the backend renders for each thread kind from its granted verbs. */
export const capabilities = {
  space: ["Answers questions about any project's graph", "Hands work to a project's own agent"],
  project: ["Answers questions about the graph and records results in it"],
};

export const message = (id, role, text, operation, extra = {}) => ({
  message_id: id,
  operation_id: operation,
  role,
  text,
  timestamp: ago(30),
  native_session_id: null,
  provider: "claude",
  model: "opus",
  reasoning: "high",
  execution_machine: null,
  applied_revision: null,
  mode: "work",
  graph_update: null,
  trigger: "human",
  attachments: [],
  turn_outcome: null,
  ...extra,
});

export const transcript = (chatId, messages) => ({
  chat_id: chatId,
  kind: "project_chat",
  node_id: null,
  title: chatId,
  updated_at: ago(0),
  message_count: messages.length,
  last_message_preview: "",
  graph_target: { kind: "main" },
  messages,
});

const command = (id, phase, second, text) => ({
  event_id: second,
  operation_id: "op-space",
  created_at: new Date(Date.UTC(2026, 10, 1, 8, 0, second)).toISOString(),
  level: "info",
  message: text ?? id,
  event_kind: "command",
  command_id: id,
  command_phase: phase,
});

export const settled = (operationId, events = []) => ({
  operation_id: operationId,
  active: false,
  settled: true,
  finished: true,
  paused: false,
  can_resume: false,
  can_retry: false,
  can_steer: false,
  steer_turn_id: null,
  status_message: "Done",
  request: {},
  events,
});

const spaceTranscript = transcript("space", [
  message("m1", "user", "Anything need me this morning?", "op-space"),
  message(
    "m2",
    "assistant",
    "**Sparse attention.** [sa-v2-lr3e4](#/projects/p1?view=runs) finished overnight. Validation loss at 32k context fell from 2.31 to 2.18. Branch `sparse-v2` is ready to merge.\n\n**Tokenizer ablations.** 2 of 4 runs are still training. I'll check them at 18:00.\n\n**Long-context eval.** Nothing new.",
    "op-space",
  ),
]);

export const mossTranscript = transcript("moss", [
  message("s1", "user", "Morning check-in", "op-check", { trigger: "schedule" }),
  message(
    "s2",
    "assistant",
    "[sa-v3-warmup](#/projects/p1?view=runs) passed step 20k with no loss spike. This supports [H2 · Warmup prevents loss spikes](#/projects/p1?view=dag).",
    "op-check",
  ),
  message("s3", "user", "Good. Mark H2 supported and start the 64k run.", "op-moss", {
    source: "voice",
  }),
  message(
    "s4",
    "assistant",
    "Started the 64k run on the GPU machine. Changing H2's standing needs your approval.",
    "op-moss",
    { timestamp: ago(8) },
  ),
]);

export const card = {
  question_id: "q1",
  owner_kind: "chat",
  owner_id: "moss",
  operation_id: "op-moss",
  question: "H2's evidence now covers both schedules.",
  choices: [],
  multiple: false,
  state: "pending",
  answer: null,
  chosen_choices: [],
  resolved_by: null,
  resolved_at: null,
  withdrawn_readonly: false,
  can_answer: true,
  created_at: ago(5),
  agent_waiting: false,
  answer_continues: null,
  capability: "work_auto",
  card: {
    kind: "proposal",
    action: { kind: "proposal", project_id: "p1", proposal_id: "pr-1", decision: "approved" },
    pinned: {
      proposal: { id: "pr-1", title: "H2 standing → supported" },
      proposal_state: "pending",
    },
    pinned_sha256: "sha-1",
    expires_at: new Date(now + 3_600_000).toISOString(),
    state: "pending",
  },
};

/** A Proposal card as `GET /api/agent-threads/cards` draws it for Moss's latest turn. */
export const proposalCard = (id, title, extra = {}) => ({
  project_id: "p1",
  project_name: "Sparse attention",
  thread_id: "moss",
  turn_id: "op-moss",
  turn_created_at: ago(9),
  proposal: {
    id,
    title,
    status: "pending",
    semantics: "canonical",
    card: {
      situation_cold: "Both warmup schedules finished without a loss spike.",
      why_human_now: "Standing changes need a member.",
      consequences: "H2 reads supported in every view.",
      decision_needed: "Approve or reject the standing change.",
    },
  },
  stale: false,
  changes: [{ label: "Standing", before: "asserted", text: "accepted" }],
  ...extra,
});

export const readiness = {
  provider: "claude",
  label: "Claude",
  installed: true,
  authenticated: true,
  binary_path: "/bin/claude",
  path_state: "resolved",
  models: [{ id: "opus", label: "opus", reasoning: ["high"] }],
  runtimes: [],
  default_runtime: "cli",
};

export const schedules = [
  {
    schedule_id: "s-a",
    at_local: "2026-11-01T18:00",
    timezone: "UTC",
    note: "Tokenizer ablations: check the 4 runs",
    state: "active",
  },
  {
    schedule_id: "s-b",
    cron: "0 9 * * 1-5",
    timezone: "UTC",
    note: "Morning summary",
    state: "active",
  },
];

/** Answers every API route the Agent view reads, and records each write. */
export async function mockApi(page, writes) {
  /** Each thread's stored agent pick, as the backend keeps it across reloads. */
  const agents = new Map();
  const withAgent = (thread) => ({
    ...thread,
    agent: agents.get(thread.thread_id) ?? null,
    capabilities: capabilities[thread.kind],
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = decodeURIComponent(url.pathname);
    const method = request.method();
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (method !== "GET") {
      writes.push({ method, path, body: request.postData() && JSON.parse(request.postData()) });
    }
    if (path === "/api/agent-threads") return json({ threads: threads.map(withAgent) });
    if (path === "/api/agent-threads/cards")
      return json({ proposal_cards: [], asked_cards: [], questions: [] });
    if (path === "/api/agent-threads/running") return json({ rows: [], needs: 0, total: 0 });
    if (/^\/api\/agent-threads\/[^/]+\/queue$/.test(path) && method === "GET")
      return json({ items: [] });
    if (/^\/api\/agent-threads\/[^/]+\/agent$/.test(path) && method === "POST") {
      const id = path.split("/").at(-2);
      agents.set(id, JSON.parse(request.postData()));
      return json(withAgent(threads.find((thread) => thread.thread_id === id)));
    }
    if (path === "/api/resource-watermarks")
      return json({
        epoch: "e",
        as_of: new Date().toISOString(),
        projects: [],
        unavailable: [],
        agent_threads: { version: "1" },
      });
    if (path === "/api/agent-threads/space/transcript") return json(spaceTranscript);
    if (path === "/api/projects/p1/chats/moss") return json(mossTranscript);
    if (/^\/api\/projects\/p\d\/chats\/[^/]+$/.test(path))
      return json(transcript(path.split("/").at(-1), []));
    if (path === "/api/projects/p1/chats/moss/questions") return json([card]);
    if (path.endsWith("/questions")) return json([]);
    if (path === "/api/agent-threads/space/tasks/op-space")
      return json(
        settled("op-space", [
          command("a", "start", 0, "status --project sparse-attention"),
          command("a", "exit", 2),
          command("b", "start", 2, "graph log sparse-v2 --since 18h"),
          command("b", "exit", 5),
          command("c", "start", 5, "compute jobs --project tokenizer-ablations"),
          command("c", "exit", 9),
          command("d", "start", 9, "tail -n 40 runs/sa-v2-lr3e4/train.log"),
          command("d", "exit", 12),
        ]),
      );
    if (/\/tasks\/[^/]+$/.test(path) && method === "GET")
      return json(settled(path.split("/").at(-1)));
    if (/^\/api\/agent-threads\/[^/]+\/schedules$/.test(path))
      return json({ schedules: path.includes("/space/") ? schedules : schedules.slice(1) });
    if (method === "DELETE") return route.fulfill({ status: 204 });
    if (path === "/api/agent-threads/space/turns") return json({ operation_id: "op-new" }, 202);
    if (/\/read$/.test(path)) {
      const id = path.split("/").at(-2);
      return json({ ...threads.find((thread) => thread.thread_id === id), unread: false });
    }
    if (path === "/api/approval-cards/q1") return json({ outcome: "approved" });
    if (path.startsWith("/api/approval-cards/proposals/") && method === "POST")
      return json({
        proposal_id: path.split("/").slice(5).join("/"),
        status: JSON.parse(request.postData()).decision,
      });
    if (/^\/api\/projects\/p\d\/readiness$/.test(path))
      return json({
        run_on: "local",
        provider_readiness: { local: { claude: readiness } },
        agent_profiles: {
          project_chat: { provider: "claude", model: "opus", reasoning: "high", run_on: "local" },
        },
      });
    if (path === "/api/space/machines")
      return json({
        machines: [{ machine_id: "local", host: "", provider_readiness: { claude: readiness } }],
      });
    return json({ detail: "not in this fixture" }, 404);
  });
}

/** Serves the Agent threads fixture page and hands each run a fresh browser. */
export async function withFixture(run) {
  const server = await createServer({
    root: new URL("../..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    await run(
      browser,
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/agentThreads.html`,
    );
  } finally {
    await browser?.close();
    await server.close();
  }
}
