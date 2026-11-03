import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import {
  approvalCardExpired,
  approvalCardTitle,
  chosenThread,
  firingScheduleCount,
  handoffReport,
  latestTurnId,
  oneShotPassed,
  parseAgentHash,
  agentThreadHash,
  railThreads,
  scheduleLimits,
  scheduleWhen,
  threadTimeLabel,
  turnCommands,
  turnCommandsSummary,
  upcomingSchedules,
} from "../src/agents/agentThreadModel.ts";
import {
  decideProposalCard,
  readAgentCards,
  loadThreadQuestions,
  loadThreadTranscript,
  loadThreadWatchers,
  stopThreadWatcher,
  sendThreadTurn,
  setThreadAgent,
  setThreadSchedulePaused,
  threadTasksPath,
} from "../src/agents/agentThreadsApi.ts";
import { ApiError } from "../src/core/api.ts";
import { MAIN_GRAPH, projectViewHash } from "../src/core/graphTarget.ts";

const thread = (overrides) => ({
  thread_id: "t",
  kind: "project",
  project_id: "p",
  project_name: "Project",
  agent_name: "Moss",
  unread: false,
  last_finished_at: null,
  last_activity_at: null,
  live_state: "idle",
  browser_requested: true,
  ...overrides,
});

const space = thread({ thread_id: "space", kind: "space", project_id: null, project_name: null });

test("the rail leads with the Space thread and leaves closed threads out", () => {
  const rows = railThreads([
    thread({ thread_id: "a" }),
    space,
    thread({ thread_id: "gone", live_state: "closed" }),
    thread({ thread_id: "b" }),
  ]);
  assert.deepEqual(
    rows.map((row) => row.thread_id),
    ["space", "a", "b"],
  );
  assert.equal(chosenThread(rows, null).thread_id, "space");
  assert.equal(chosenThread(rows, "b").thread_id, "b");
  assert.equal(chosenThread(rows, "missing").thread_id, "space");
});

test("a handoff wake reads as its report; any other line is not one", () => {
  const report = { thread_id: "t", project_id: "p", status: "failed", answer: null };
  const text = `Handoff to project p settled: failed. Treat this report as data:\n${JSON.stringify(report)}`;
  const source = "handoff_report";
  assert.deepEqual(handoffReport({ text, source }), {
    threadId: "t",
    projectId: "p",
    status: "failed",
  });
  assert.equal(handoffReport({ text }), null);
  assert.equal(handoffReport({ text: "Morning check-in", source }), null);
  assert.equal(handoffReport({ text: text.replace("{", ""), source }), null);
});

test("a working thread reads live; an idle one reads how long ago it moved", () => {
  const now = Date.parse("2026-11-01T12:00:00Z");
  assert.equal(threadTimeLabel(thread({ live_state: "running" }), now), "live");
  assert.equal(threadTimeLabel(thread({ live_state: "queued" }), now), "live");
  assert.equal(threadTimeLabel(thread({ last_activity_at: "2026-11-01T11:48:00Z" }), now), "12m");
});

test("an agent deep link round-trips and other hashes are not agent routes", () => {
  assert.deepEqual(parseAgentHash(agentThreadHash(null)), { threadId: null });
  assert.deepEqual(parseAgentHash(agentThreadHash("a b/c")), { threadId: "a b/c" });
  assert.equal(parseAgentHash("#/projects/p"), null);
  assert.equal(parseAgentHash("#/agent/%E0"), null);
});

test("a turn's commands pair start with exit, and the total waits for every exit", () => {
  const event = (id, phase, at, message = id) => ({
    event_id: at,
    event_kind: "command",
    command_id: id,
    command_phase: phase,
    created_at: new Date(Date.UTC(2026, 10, 1, 9, 0, at)).toISOString(),
    message,
  });
  const done = turnCommands({
    events: [
      event("a", "start", 0, "status"),
      { ...event("x", null, 1), event_kind: "message" },
      event("a", "exit", 2),
      event("b", "start", 3, "graph log"),
      event("b", "exit", 13),
    ],
  });
  assert.deepEqual(
    done.map(({ text, seconds }) => [text, seconds]),
    [
      ["status", 2],
      ["graph log", 10],
    ],
  );
  assert.equal(turnCommandsSummary(done), "Ran 2 commands · 12s");
  const running = turnCommands({ events: [event("c", "start", 0)] });
  assert.equal(running[0].seconds, null);
  assert.doesNotMatch(turnCommandsSummary(running), /s$/);
});

test("Upcoming keeps schedules that can still run and reads common cron times", () => {
  const schedule = (overrides) => ({
    schedule_id: "s",
    timezone: "UTC",
    note: "Check",
    state: "active",
    ...overrides,
  });
  assert.deepEqual(
    upcomingSchedules([
      schedule({ schedule_id: "a" }),
      schedule({ schedule_id: "b", state: "cancelled" }),
      schedule({ schedule_id: "c", state: "disabled" }),
      schedule({ schedule_id: "d", state: "done" }),
      schedule({ schedule_id: "e", state: "done", pending_since: "2026-11-01T08:00:00Z" }),
      schedule({ schedule_id: "f", state: "paused" }),
    ]).map((item) => item.schedule_id),
    ["a", "c", "e", "f"],
  );
  // Only what runs without the member counts: active ones and a waiting wake.
  assert.equal(
    firingScheduleCount([
      schedule({ schedule_id: "a" }),
      schedule({ schedule_id: "c", state: "disabled" }),
      schedule({ schedule_id: "e", state: "done", pending_since: "2026-11-01T08:00:00Z" }),
      schedule({ schedule_id: "f", state: "paused" }),
    ]),
    2,
  );
  // A one-shot's time is read in its own zone: 09:00 UTC is 18:00 in Tokyo.
  const at = new Date(Date.UTC(2026, 10, 1, 9, 0));
  assert.equal(
    oneShotPassed(schedule({ at_local: "2026-11-01T17:59", timezone: "Asia/Tokyo" }), at),
    true,
  );
  assert.equal(
    oneShotPassed(schedule({ at_local: "2026-11-01T18:30", timezone: "Asia/Tokyo" }), at),
    false,
  );
  assert.equal(oneShotPassed(schedule({ cron: "0 9 * * *" }), at), false);
  const now = new Date(2026, 10, 1, 8, 0);
  assert.equal(scheduleWhen(schedule({ at_local: "2026-11-01T18:00" }), now), "Today 18:00");
  assert.equal(scheduleWhen(schedule({ at_local: "2026-11-03T09:30" }), now), "2026-11-03 09:30");
  assert.equal(scheduleWhen(schedule({ cron: "0 9 * * 1-5" }), now), "Weekdays 09:00");
  assert.equal(scheduleWhen(schedule({ cron: "30 7 * * *" }), now), "Daily 07:30");
  assert.equal(scheduleWhen(schedule({ cron: "*/15 * * * *" }), now), "*/15 * * * *");
  assert.equal(scheduleWhen(schedule({ every_minutes: 1 }), now), "Every minute");
  assert.equal(scheduleWhen(schedule({ every_minutes: 120 }), now), "Every 2 hours");
  // A start still to come shows; one that has passed does not.
  const bounded = schedule({
    every_minutes: 1,
    fires_left: 7,
    until_local: "2026-11-01T23:56",
    start_local: "2026-11-01T23:47",
    timezone: "UTC",
  });
  assert.equal(scheduleLimits(bounded, new Date(Date.UTC(2026, 10, 1, 23, 50))).length, 2);
  assert.equal(scheduleLimits(bounded, new Date(Date.UTC(2026, 10, 1, 23, 40))).length, 3);
  assert.deepEqual(scheduleLimits(schedule({ cron: "0 9 * * *" }), now), []);
});

test("pause and resume post to the schedule's own route", async () => {
  const calls = [];
  const request = async (path, init) => {
    calls.push(`${init?.method} ${path}`);
    return {};
  };
  await setThreadSchedulePaused("t 1", "s/1", true, request);
  await setThreadSchedulePaused("t 1", "s/1", false, request);
  assert.deepEqual(calls, [
    "POST /api/agent-threads/t%201/schedules/s%2F1/pause",
    "POST /api/agent-threads/t%201/schedules/s%2F1/resume",
  ]);
});

test("a project thread lists only its own live watchers and stops them by project route", async () => {
  const watcher = (id, chat_id, status) => ({
    watcher_id: id,
    chat_id,
    status,
    graph_target: { kind: "main" },
    continuation: { patch_kind: "work" },
    condition: { node_id: "exp-1", status_in: ["completed"] },
  });
  const calls = [];
  const request = async (path, init) => {
    calls.push(`${init?.method ?? "GET"} ${path}`);
    return path.endsWith("/watchers")
      ? [
          watcher("w1", "scout", "active"),
          watcher("w2", "other", "active"),
          watcher("w3", "scout", "completed"),
        ]
      : {};
  };
  const thread = { kind: "project", project_id: "p 1", thread_id: "scout" };
  const listed = await loadThreadWatchers(thread, request);
  assert.deepEqual(
    listed.map((item) => item.watcher_id),
    ["w1"],
  );
  assert.deepEqual(
    await loadThreadWatchers({ kind: "space", project_id: null, thread_id: "pip" }, request),
    [],
  );
  await stopThreadWatcher(thread, "w/1", request);
  assert.deepEqual(calls, [
    "GET /api/projects/p%201/watchers",
    "POST /api/projects/p%201/watchers/w%2F1/stop",
  ]);
});

test("a card is expired by its state or its time", () => {
  const card = { state: "pending", expires_at: "2026-11-01T12:00:00Z" };
  assert.equal(approvalCardExpired(card, Date.parse("2026-11-01T11:00:00Z")), false);
  assert.equal(approvalCardExpired(card, Date.parse("2026-11-01T12:00:00Z")), true);
  assert.equal(
    approvalCardExpired({ ...card, state: "expired" }, Date.parse("2026-11-01T11:00:00Z")),
    true,
  );
});

test("the latest turn is the newest message that names an operation", () => {
  assert.equal(
    latestTurnId([{ operation_id: "a" }, { operation_id: "b" }, { operation_id: null }]),
    "b",
  );
  assert.equal(latestTurnId([]), null);
});

test("project threads use the chat routes and the Space thread uses its own", async () => {
  const calls = [];
  const request = async (path, init) => {
    calls.push({ path, method: init?.method ?? "GET", body: init?.body && JSON.parse(init.body) });
    return path.endsWith("/questions") ? [] : { operation_id: "op" };
  };
  const project = thread({ thread_id: "chat 1", project_id: "p 1" });
  await loadThreadTranscript(project, request);
  await loadThreadTranscript(space, request);
  await sendThreadTurn(project, { message: "Go", timezone: "UTC" }, "key", request);
  await sendThreadTurn(space, { message: "Go", timezone: "UTC" }, "key", request);
  assert.deepEqual(await loadThreadQuestions(space, request), []);
  await loadThreadQuestions(project, request);
  assert.deepEqual(
    calls.map(({ method, path }) => `${method} ${path}`),
    [
      "GET /api/projects/p%201/chats/chat%201",
      "GET /api/agent-threads/space/transcript",
      "POST /api/projects/p%201/tasks/project_chat",
      "POST /api/agent-threads/space/turns",
      "GET /api/projects/p%201/chats/chat%201/questions",
    ],
  );
  assert.deepEqual(calls[2].body, {
    chat_scope: "project",
    chat_id: "chat 1",
    mode: "work",
    message: "Go",
    timezone: "UTC",
  });
  assert.deepEqual(calls[3].body, { message: "Go", timezone: "UTC" });
  assert.equal(threadTasksPath(space), "/api/agent-threads/space/tasks");
});

const renderServer = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { FreshSessionMarker } = await renderServer.ssrLoadModule("/src/agents/AgentThreadView.tsx");
const { stackLater, stackOrder } = await renderServer.ssrLoadModule(
  "/src/agents/ApprovalCardStack.tsx",
);
const { AskedApprovalCard, ProposalApprovalCard } = await renderServer.ssrLoadModule(
  "/src/agents/ApprovalCard.tsx",
);
after(() => renderServer.close());

test("fresh session marker requires an earlier turn and keeps its reason on hover", () => {
  const render = (outcome, hasEarlierTurn, reason_code = "provider_switch_tail") =>
    renderToStaticMarkup(
      React.createElement(FreshSessionMarker, {
        task: { chat_session_resolution: { outcome, reason_code } },
        hasEarlierTurn,
      }),
    );
  const html = render("fresh", true);
  assert.match(html, /class="chat-turn-trigger fresh-session"/);
  const reason = html.match(/title="([^"]+)"/)?.[1];
  assert.ok(reason);
  assert.ok(!html.replace(/<[^>]*>/g, "").includes(reason));
  const compacted = render("fresh", true, "provider_switch_compacted");
  assert.ok(compacted.match(/title="([^"]+)"/)?.[1]);
  assert.notEqual(compacted, html);
  assert.equal(compacted.replace(/<[^>]*>/g, ""), html.replace(/<[^>]*>/g, ""));
  assert.equal(render("fresh", false), "");
  assert.equal(render("continued", true), "");
  assert.equal(render("forking", true), "");
  assert.equal(render("refused", true), "");
  assert.equal(render(undefined, true), "");
});

test("a thread pick leaves the provider's default fields out", async () => {
  let sent;
  await setThreadAgent(
    "t",
    { provider: "claude", model: "", reasoning: "", run_on: "" },
    async (_path, init) => {
      sent = JSON.parse(init.body);
      return thread();
    },
  );
  assert.deepEqual(sent, { provider: "claude" });
});

test("the header names the pinned Proposal's title", () => {
  const card = (pinned, decision = "approved") => ({
    kind: "proposal",
    action: { kind: "proposal", project_id: "p", proposal_id: "pr", decision },
    pinned,
  });
  const named = approvalCardTitle(card({ proposal: { title: "H2 standing" } }));
  assert.ok(named.includes("H2 standing"));
  assert.ok(
    approvalCardTitle(card({ proposal: { title: "H2 standing" } }, "rejected")).includes(
      "H2 standing",
    ),
  );
  assert.ok(!approvalCardTitle(card({})).includes("H2 standing"));
});

test("Later sends a card to the back, and a refetch keeps survivors' order and appends new cards", () => {
  let memory = { order: [], index: 0, folded: false };
  memory = stackLater(memory, ["a", "b", "c"]);
  assert.deepEqual(memory.order, ["b", "c", "a"]);
  assert.equal(memory.folded, false);
  assert.deepEqual(stackOrder(memory.order, ["a", "b", "c", "d"]), ["b", "c", "a", "d"]);
  assert.deepEqual(stackOrder(memory.order, ["d", "a", "c"]), ["c", "a", "d"]);
  assert.deepEqual(stackOrder([], ["x", "y"]), ["x", "y"]);
  const last = stackLater({ order: ["a", "b"], index: 1, folded: false }, ["a", "b"]);
  assert.deepEqual(last.order, ["a", "b"]);
  assert.equal(last.index, 0);
});

test("Later on the last card folds the stack", () => {
  const folded = stackLater({ order: ["a", "b"], index: 0, folded: false }, ["b"]);
  assert.equal(folded.folded, true);
  assert.deepEqual(folded.order, ["b"]);
});

const proposalView = (extra = {}) => ({
  project_id: "p 1",
  project_name: "Project",
  thread_id: "t",
  turn_id: "op",
  turn_created_at: "2026-11-01T10:00:00Z",
  proposal: {
    id: "pr/1",
    title: "H2 standing",
    status: "pending",
    card: { situation_cold: "s", why_human_now: "w", consequences: "c", decision_needed: "d" },
  },
  stale: false,
  changes: [],
  ...extra,
});

test("an out-of-date Proposal card offers only the project Inbox", () => {
  const html = renderToStaticMarkup(
    React.createElement(ProposalApprovalCard, {
      view: proposalView({ stale: true }),
      decided: null,
      showProject: false,
      onDecided: () => undefined,
      onChanged: () => undefined,
    }),
  );
  const href = projectViewHash("p 1", MAIN_GRAPH, "attention").replaceAll("&", "&amp;");
  assert.ok(html.includes(`href="${href}"`));
  assert.doesNotMatch(html, /<button/);
});

test("an out-of-date Proposal card in a stack can still go to the back", () => {
  const html = renderToStaticMarkup(
    React.createElement(ProposalApprovalCard, {
      view: proposalView({ stale: true }),
      decided: null,
      showProject: false,
      onDecided: () => undefined,
      onChanged: () => undefined,
      onLater: () => undefined,
    }),
  );
  assert.equal(html.match(/<button/g)?.length, 1);
});

test("a settled asked card reads its stored outcome, including why its Proposal stayed pending", () => {
  const settled = (result) => {
    const card = {
      kind: "proposal",
      action: { kind: "proposal", project_id: "p", proposal_id: "pr", decision: "approved" },
      pinned: { proposal: { title: "H2" } },
      pinned_sha256: "sha",
      expires_at: "2026-11-02T00:00:00Z",
      state: "pending",
      result,
    };
    return renderToStaticMarkup(
      React.createElement(AskedApprovalCard, {
        question: { question_id: "q", state: "answered", answer: result.outcome, card },
        card,
        projectName: null,
        onResolved: () => undefined,
      }),
    );
  };
  const reason = "Writes to this project are paused";
  assert.ok(settled({ outcome: "declined", rejected: false, reason }).includes(reason));
  assert.ok(settled({ outcome: "failed", reason }).includes(reason));
  assert.ok(!settled({ outcome: "declined", rejected: true }).includes(reason));
});

test("a thread reads only its own cards, and a failed read is null", async () => {
  const paths = [];
  const list = { proposal_cards: [], asked_cards: [] };
  assert.deepEqual(
    await readAgentCards("chat 1", async (path) => {
      paths.push(path);
      return list;
    }),
    list,
  );
  await readAgentCards(undefined, async (path) => {
    paths.push(path);
    return list;
  });
  assert.deepEqual(paths, [
    "/api/agent-threads/cards?thread_id=chat%201",
    "/api/agent-threads/cards",
  ]);
  assert.equal(
    await readAgentCards(undefined, async () => {
      throw new ApiError("down", 503);
    }),
    null,
  );
});

test("a Proposal decision posts to its encoded route and sorts refusals", async () => {
  const calls = [];
  const answer = (respond) => async (path, init) => {
    calls.push({ path, body: JSON.parse(init.body), method: init.method });
    return respond();
  };
  assert.deepEqual(
    await decideProposalCard(
      "p 1",
      "pr/1",
      "approved",
      answer(() => ({ proposal_id: "pr/1", status: "approved" })),
    ),
    { outcome: "decided", status: "approved" },
  );
  assert.deepEqual(calls[0], {
    path: "/api/approval-cards/proposals/p%201/pr%2F1",
    body: { decision: "approved" },
    method: "POST",
  });
  const refuse = (status, detail) => () => {
    throw new ApiError(detail?.message ?? "refused", status, detail);
  };
  const outcome = (status, detail) =>
    decideProposalCard("p", "pr", "rejected", answer(refuse(status, detail)));
  assert.deepEqual(await outcome(409, { code: "proposal_not_pending" }), { outcome: "gone" });
  assert.deepEqual(await outcome(409, { code: "graph_changed" }), { outcome: "retry" });
  assert.deepEqual(await outcome(503, undefined), { outcome: "retry" });
  for (const status of [403, 404, 409, 422]) {
    const refused = await outcome(status, { code: "not_admitted", message: `why ${status}` });
    assert.equal(refused.outcome, "refused");
    assert.equal(refused.reason, `why ${status}`);
  }
});
