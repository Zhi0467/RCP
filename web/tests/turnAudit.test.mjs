import assert from "node:assert/strict";
import test from "node:test";
import { withTaskAnswers } from "./taskAnswers.mjs";

import {
  taskOwnerRows,
  turnAttempts,
  turnEndLineIds,
  turnTitle,
} from "../src/agents/agentTasks.ts";

function task(overrides) {
  return withTaskAnswers({
    project_id: "project",
    kind: "node_chat",
    status: "succeeded",
    request: {},
    created_at: "2026-11-20T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    status_message: "Done",
    attempt: 1,
    estimate_seconds: 1,
    estimate_samples: 0,
    phase: "done",
    elapsed_seconds: 1,
    progress: 1,
    can_pause: false,
    can_resume: false,
    can_retry: false,
    ...overrides,
  });
}

test("owners: a listed chat is one agent row however many turns it has", () => {
  const tasks = [
    task({ operation_id: "a", request: { chat_id: "chat-1" }, updated_at: "2026-11-20T10:00:00Z" }),
    task({ operation_id: "b", request: { chat_id: "chat-1" }, updated_at: "2026-11-20T11:00:00Z" }),
  ];
  const rows = taskOwnerRows(tasks, new Map([["chat-1", "Ablation plan"]]));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "agent");
  assert.equal(rows[0].chatId, "chat-1");
  assert.equal(rows[0].label, "Ablation plan");
  assert.equal(rows[0].latest.operation_id, "b");
});

test("owners: an episode's turns are one run row even when they name unlisted chats", () => {
  const tasks = [
    task({
      operation_id: "w1",
      episode_id: "ep",
      request: { chat_id: "worker-1", trigger: "orchestrator" },
    }),
    task({
      operation_id: "o1",
      kind: "auto_research",
      episode_id: "ep",
      request: { role: "orchestrator" },
    }),
    task({
      operation_id: "x1",
      episode_id: "ep2",
      request: { chat_id: "exp", trigger: "experiment_run" },
    }),
    task({
      operation_id: "x2",
      episode_id: "ep2",
      request: { chat_id: "exp", trigger: "watcher" },
      updated_at: "2026-11-20T12:00:00Z",
    }),
  ];
  const rows = taskOwnerRows(tasks, new Map());
  assert.deepEqual(rows.map((row) => [row.kind, row.key]).sort(), [
    ["episode", "episode:ep"],
    ["episode", "episode:ep2"],
  ]);
  assert.notEqual(
    rows.find((row) => row.key === "episode:ep").label,
    rows.find((row) => row.key === "episode:ep2").label,
  );
});

test("owners: a listed chat wins over its episode", () => {
  const tasks = [task({ operation_id: "x", episode_id: "ep", request: { chat_id: "exp" } })];
  const [row] = taskOwnerRows(tasks, new Map([["exp", "lr sweep"]]));
  assert.equal(row.kind, "agent");
});

test("owners: an orchestrator's chained episodes are one agent row when Agents lists it", () => {
  const orchestrator = (id, episode) =>
    task({
      operation_id: id,
      kind: "auto_research",
      episode_id: episode,
      request: { role: "orchestrator" },
      graph_target: { kind: "branch", branch_id: "ar-branch" },
    });
  const rows = taskOwnerRows(
    [orchestrator("o1", "ep1"), orchestrator("o2", "ep2")],
    new Map([["ar-branch", "Auto-research"]]),
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "agent");
});

test("owners: an unlisted chat still groups by its id but opens no conversation", () => {
  const tasks = [
    task({ operation_id: "c1", kind: "project_chat", request: { chat_id: "consolidation" } }),
    task({ operation_id: "c2", kind: "project_chat", request: { chat_id: "consolidation" } }),
  ];
  const rows = taskOwnerRows(tasks, new Map());
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, "run");
  assert.equal(rows[0].chatId, null);
});

test("owners: a run with no chat or episode is one row per Retry lineage", () => {
  const tasks = [
    task({ operation_id: "m1", kind: "branch_merge", status: "failed" }),
    task({ operation_id: "m2", kind: "branch_merge", parent_operation_id: "m1", attempt: 2 }),
    task({ operation_id: "m3", kind: "branch_merge" }),
  ];
  const rows = taskOwnerRows(tasks, new Map());
  assert.deepEqual(rows.map((row) => row.key).sort(), ["run:m1", "run:m3"]);
  assert.equal(rows.find((row) => row.key === "run:m1").latest.operation_id, "m2");
});

test("owners: rows sort by their newest turn's activity", () => {
  const tasks = [
    task({ operation_id: "old", request: { chat_id: "a" }, updated_at: "2026-11-20T09:00:00Z" }),
    task({ operation_id: "new", kind: "branch_merge", updated_at: "2026-11-20T12:00:00Z" }),
  ];
  assert.deepEqual(
    taskOwnerRows(tasks, new Map([["a", "A"]])).map((row) => row.latest.operation_id),
    ["new", "old"],
  );
});

test("attempts: the whole Retry and Resume lineage, newest first, from any member", () => {
  const tasks = [
    task({ operation_id: "t1", attempt: 1 }),
    task({ operation_id: "t2", attempt: 2, parent_operation_id: "t1" }),
    task({ operation_id: "t3", attempt: 3, parent_operation_id: "t2" }),
    task({ operation_id: "other", attempt: 1 }),
  ];
  for (const id of ["t1", "t2", "t3"]) {
    const selected = tasks.find((item) => item.operation_id === id);
    assert.deepEqual(
      turnAttempts(selected, tasks).map((item) => item.operation_id),
      ["t3", "t2", "t1"],
    );
  }
  assert.deepEqual(
    turnAttempts(tasks[3], tasks).map((item) => item.operation_id),
    ["other"],
  );
});

test("attempts: a wake or kickoff that names a parent is not another attempt", () => {
  const tasks = [
    task({ operation_id: "turn", kind: "auto_research", attempt: 1 }),
    task({ operation_id: "wake", kind: "auto_research", attempt: 1, parent_operation_id: "turn" }),
    task({ operation_id: "kickoff", kind: "node_chat", attempt: 2, parent_operation_id: "turn" }),
  ];
  for (const selected of tasks) {
    assert.deepEqual(
      turnAttempts(selected, tasks).map((item) => item.operation_id),
      [selected.operation_id],
    );
  }
});

test("attempts: a detail missing from the list is still its own attempt", () => {
  const detail = task({ operation_id: "solo" });
  assert.deepEqual(
    turnAttempts(detail, []).map((item) => item.operation_id),
    ["solo"],
  );
});

test("turn title: the member's first line, else the kind", () => {
  assert.equal(
    turnTitle(task({ operation_id: "a", request: { message: "Check the gap\nmore detail" } })),
    "Check the gap",
  );
  const untitled = turnTitle(task({ operation_id: "b", kind: "branch_merge" }));
  assert.ok(untitled.length > 0);
});

test("turn end: each turn's last agent-side line, never the member's", () => {
  const line = (lineId, taskId, role) => ({ lineId, taskId, role, text: "", timestamp: "" });
  const ends = turnEndLineIds([
    line("h1", "t1", "human"),
    line("a1", "t1", "agent"),
    line("e1", "t1", "error"),
    line("h2", "t2", "human"),
  ]);
  assert.deepEqual([...ends], ["e1"]);
});
