import assert from "node:assert/strict";
import test from "node:test";

import {
  approvalCardExpired,
  chosenThread,
  latestTurnId,
  parseAgentHash,
  agentThreadHash,
  railThreads,
  scheduleWhen,
  threadTimeLabel,
  turnCommands,
  turnCommandsSummary,
  upcomingSchedules,
} from "../src/agents/agentThreadModel.ts";
import {
  loadThreadQuestions,
  loadThreadTranscript,
  sendThreadTurn,
  threadTasksPath,
} from "../src/agents/agentThreadsApi.ts";

const thread = (overrides) => ({
  thread_id: "t",
  kind: "project",
  project_id: "p",
  project_name: "Project",
  agent_name: "Moss",
  unread: false,
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
    ]).map((item) => item.schedule_id),
    ["a", "c"],
  );
  const now = new Date(2026, 10, 1, 8, 0);
  assert.equal(scheduleWhen(schedule({ at_local: "2026-11-01T18:00" }), now), "Today 18:00");
  assert.equal(scheduleWhen(schedule({ at_local: "2026-11-03T09:30" }), now), "2026-11-03 09:30");
  assert.equal(scheduleWhen(schedule({ cron: "0 9 * * 1-5" }), now), "Weekdays 09:00");
  assert.equal(scheduleWhen(schedule({ cron: "30 7 * * *" }), now), "Daily 07:30");
  assert.equal(scheduleWhen(schedule({ cron: "*/15 * * * *" }), now), "*/15 * * * *");
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
