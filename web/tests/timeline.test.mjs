import assert from "node:assert/strict";
import test from "node:test";
import {
  timelineRows,
  timelineBounds,
  clampWindow,
  zoomWindow,
  panWindow,
  timelineRelated,
  timelineWakeRows,
  timelineSummary,
} from "../src/timeline.ts";

const at = (minutes) => new Date(Date.UTC(2026, 0, 1, 0, minutes)).toISOString();
const actor = (id, kind, row = id, extra = {}) => ({
  actor_id: id,
  kind,
  row_key: row,
  label: id,
  subtitle: null,
  started_at: at(0),
  ended_at: at(120),
  outcome: "completed",
  started_by_span_id: null,
  links: {},
  ...extra,
});
const span = (id, actorId, minute = 0) => ({
  span_id: id,
  actor_id: actorId,
  kind: "turn",
  started_at: at(minute),
  finished_at: at(minute + 2),
  status: "succeeded",
  task_id: id,
  headline: null,
});
const data = (extra = {}) => ({
  episode_id: "run",
  mode: "auto_research",
  generated_at: at(150),
  truncated: false,
  members: [{ episode_id: "run", started_at: at(0), ended_at: at(120) }],
  actors: [],
  spans: [],
  handoffs: [],
  messages: [],
  signals: [],
  marks: [],
  ...extra,
});

test("actors group by recorded row key in kind and start order; actorless watcher nodes get a row", () => {
  const rows = timelineRows(
    data({
      actors: [
        actor("late", "experiment", "node", {
          started_at: at(30),
          links: { episode_id: "latest", control_node_id: "node" },
        }),
        actor("worker", "worker", "worker", { label: "Assignment: Inspect results" }),
        actor("early", "experiment", "node", {
          links: { episode_id: "earlier", control_node_id: "node" },
        }),
        actor("person", "human"),
        actor("w1", "watcher", "group"),
        actor("w2", "watcher", "group"),
      ],
      signals: [{ item_id: "signal:g", kind: "watcher", source_row_key: "node:orphan" }],
    }),
    [
      { episode: { episode_id: "latest" }, node: { title: "Latest experiment" } },
      { episode: { episode_id: "earlier" }, node: { title: "Earlier experiment" } },
    ],
  );
  assert.equal(rows[1].label, "Inspect results");
  assert.equal(rows[2].label, "Latest experiment");
  assert.equal(rows[2].subtitle, "node");
  assert.deepEqual(
    rows.map((r) => r.rowKey),
    ["person", "worker", "node", "group", "node:orphan"],
  );
  assert.equal(rows[4].label, "orphan");
  assert.deepEqual(
    rows[2].actors.map((a) => a.actor_id),
    ["early", "late"],
  );
  assert.equal(rows[3].actors.length, 2);
});

test("time windows honor run bounds, ten minute minimum and cursor anchored zoom", () => {
  const run = data();
  const bounds = timelineBounds(run);
  const minute = 60_000;
  assert.deepEqual(bounds, [Date.parse(at(0)), Date.parse(at(120))]);
  assert.equal(
    timelineBounds(data({ members: [{ started_at: at(0), ended_at: null }] }))[1],
    Date.parse(at(150)),
  );
  const simple = [0, 120 * minute];
  assert.deepEqual(zoomWindow(simple, simple, 30 * minute, 0.5), [15 * minute, 75 * minute]);
  assert.deepEqual(clampWindow([-minute, minute], simple), [0, 10 * minute]);
  assert.deepEqual(panWindow([20 * minute, 40 * minute], simple, 200 * minute), [
    100 * minute,
    120 * minute,
  ]);
  assert.deepEqual(zoomWindow(simple, simple, 60 * minute, 5), simple);
});

test("relations keep recorded direct joins, including absent and node-row endpoints", () => {
  const run = data({
    actors: [
      actor("orchestrator", "orchestrator"),
      actor("child", "experiment", "node", { started_by_span_id: "outside" }),
    ],
    spans: [span("turn", "orchestrator"), span("unrelated", "orchestrator", 30)],
    handoffs: [{ item_id: "handoff", from_span_id: "outside", to_actor_id: "child" }],
    signals: [
      {
        item_id: "watch",
        kind: "watcher",
        source_actor_id: null,
        source_row_key: "node",
        landed_span_id: "turn",
        armed_span_id: null,
      },
    ],
    marks: [{ item_id: "stop", actor_id: "child", by_span_id: null }],
  });
  const related = timelineRelated(run, "child");
  for (const id of ["child", "outside", "handoff", "watch", "turn", "stop"])
    assert.ok(related.has(id), id);
  assert.ok(!related.has("unrelated"));
  assert.deepEqual([...timelineRelated(run, "stop")].sort(), ["child", "stop"]);
});

test("wake table derives consumption and actions from span IDs", () => {
  const run = data({
    actors: [actor("o", "orchestrator"), actor("worker", "worker")],
    spans: [span("second", "o", 30), span("worker-turn", "worker", 2), span("first", "o")],
    messages: [
      { item_id: "received", delivered_span_id: "second", sent_span_id: "first" },
      { item_id: "failed", delivered_span_id: null, sent_span_id: null },
    ],
    signals: [
      {
        item_id: "signal",
        kind: "watcher",
        source_row_key: "node:test",
        landed_span_id: "second",
        armed_span_id: "first",
      },
    ],
    handoffs: [{ item_id: "assignment", from_span_id: "first" }],
  });
  const rows = timelineWakeRows(run);
  assert.deepEqual(
    rows.map((r) => [r.number, r.span.span_id]),
    [
      [1, "first"],
      [2, "second"],
    ],
  );
  assert.deepEqual(
    rows[1].landed.map((i) => i.item_id),
    ["signal", "received"],
  );
  assert.deepEqual(
    rows[0].actions.map((i) => i.item_id),
    ["assignment", "received", "signal"],
  );
});

test("summary partitions every shown actor and message exactly once", () => {
  const dispositions = ["wake", "harvested", "cleared", "failed_attempt", "undelivered", "unknown"];
  const run = data({
    truncated: true,
    actors: [
      actor("a", "worker"),
      actor("b", "worker", "b", { outcome: "failed" }),
      actor("c", "human", "c", { outcome: null }),
    ],
    messages: dispositions.map((disposition) => ({ disposition })),
    handoffs: [{}],
  });
  const summary = timelineSummary(run);
  assert.equal(
    summary.actors.reduce((n, k) => n + k.total, 0),
    summary.totalActors,
  );
  for (const kind of summary.actors)
    assert.equal(
      Object.values(kind.outcomes).reduce((a, b) => a + b, 0),
      kind.total,
    );
  assert.equal(
    Object.values(summary.messages).reduce((a, b) => a + b, 0),
    summary.totalMessages,
  );
  assert.equal(summary.totalMessages, 6);
  assert.equal(summary.handoffs, 1);
  assert.equal(summary.truncated, true);
});

test("summary treats unfinished actors as running and exhaustion as out of turns", () => {
  const summary = timelineSummary(
    data({
      actors: [
        actor("live", "experiment", "live", { outcome: null, ended_at: null }),
        actor("spent", "experiment", "spent", { outcome: "exhausted" }),
        actor("ended", "experiment", "ended", { outcome: null }),
      ],
    }),
  );
  assert.deepEqual(summary.actors[0].outcomes, { running: 1, "out of turns": 1, unknown: 1 });
});

test("wake wording identifies sources and distinguishes arming from firing", () => {
  const run = data({
    actors: [
      actor("o", "orchestrator"),
      actor("worker", "worker", "worker", { label: "Assignment: Inspect results" }),
      actor("child", "experiment", "node:test", {
        links: { control_node_id: "test", episode_id: "child-episode" },
      }),
    ],
    spans: ["fresh", "lifecycle", "graph_condition", "message", "custom"].map((cause, index) => ({
      ...span(cause, "o", index),
      cause,
    })),
    handoffs: [
      {
        item_id: "assign",
        kind: "assignment",
        from_span_id: "fresh",
        to_actor_id: "worker",
        at: at(0),
      },
      { item_id: "goal", kind: "goal", from_span_id: "fresh", to_actor_id: "child", at: at(1) },
    ],
    messages: [
      {
        item_id: "mail",
        from_actor_id: "worker",
        to_actor_id: "o",
        delivered_span_id: "message",
        sent_span_id: "fresh",
        sent_at: at(2),
        disposition: "wake",
      },
    ],
    signals: [
      {
        item_id: "notice",
        kind: "notice",
        source_actor_id: "worker",
        source_row_key: "worker",
        event: "succeeded",
        landing: "woke",
        landed_span_id: "lifecycle",
      },
      {
        item_id: "watch",
        kind: "watcher",
        source_actor_id: null,
        source_row_key: "node:test",
        event: "fired",
        landing: "woke",
        landed_span_id: "graph_condition",
        armed_span_id: "fresh",
        armed_at: at(3),
      },
    ],
  });
  const rows = timelineWakeRows(run, [
    { episode: { episode_id: "child-episode" }, node: { title: "Control experiment" } },
  ]);
  assert.deepEqual(
    rows.map((r) => r.cause),
    ["Started", "Lifecycle notice", "Watcher fired", "Message", "custom"],
  );
  assert.deepEqual(
    rows.flatMap((r) => r.landed.map((i) => i.label)),
    [
      "Inspect results · succeeded · woke",
      "Watcher on test · fired · woke",
      "Message from Inspect results · delivered with a wake",
    ],
  );
  assert.deepEqual(
    rows[0].actions.map((i) => [i.label, i.icon]),
    [
      ["Started worker Inspect results", "document"],
      ["Started Experiment Control experiment", "document"],
      ["Sent message to o", "envelope"],
      ["Armed watcher on test", null],
    ],
  );
});
