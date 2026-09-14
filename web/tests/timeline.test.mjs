import assert from "node:assert/strict";
import test from "node:test";
import { EpisodeTimeline, DEFAULT_TIMELINE_CONFIG } from "../src/timeline.ts";
import { fetchEpisodeTimeline } from "../src/api.ts";

function event(event_id, kind, minute, parent_event_id = null, actor = "orchestrator") {
  return {
    event_id,
    kind,
    at: `2026-09-14T10:${String(minute).padStart(2, "0")}:00Z`,
    actor: { kind: actor, id: null, label: actor, member: null },
    parent_event_id,
    title: event_id,
    detail: null,
    status: null,
    cause: null,
    links: {
      task_id: null,
      message_id: null,
      notice_id: null,
      episode_id: null,
      control_node_id: null,
    },
    provenance: "recorded",
  };
}

test("sorts without mutating input and nests causal records in time order", () => {
  const events = [
    event("notice:b", "notice", 3, "wake:one"),
    event("retry:r", "retry", 5, "turn:root"),
    event("turn:root", "turn", 0),
    event("mail:m", "mail", 3, "wake:one"),
    event("wake:one", "wake", 2),
    event("turn:worker", "turn", 1, "turn:root", "worker"),
    event("child:c:created", "child", 4, "turn:root", "child"),
    event("human:start", "human", 0, null, "human"),
  ];
  const original = [...events];
  const timeline = new EpisodeTimeline(events);
  assert.deepEqual(events, original);
  assert.deepEqual(
    timeline.events.map((item) => item.event_id),
    [
      "human:start",
      "turn:root",
      "turn:worker",
      "wake:one",
      "mail:m",
      "notice:b",
      "child:c:created",
      "retry:r",
    ],
  );
  assert.deepEqual(
    timeline
      .rows()
      .map((row) => [row.event.event_id, row.children.map((child) => child.event.event_id)]),
    [
      ["human:start", []],
      ["turn:root", ["turn:worker", "child:c:created", "retry:r"]],
      ["wake:one", ["mail:m", "notice:b"]],
    ],
  );
  assert.deepEqual(timeline.lanes(), ["orchestrator", "workers", "children", "mail", "human"]);
  assert.deepEqual(timeline.summary(), { turns: 2, wakes: 1, retries: 1, mail: 1, children: 1 });
});

test("configuration controls lane order, glyph, fold defaults, and status tone", () => {
  const mail = event("mail:m", "mail", 1);
  const worker = { ...event("turn:w", "turn", 0, null, "worker"), status: "running" };
  const defaultTimeline = new EpisodeTimeline([mail, worker]);
  assert.equal(defaultTimeline.rule(worker).lane, "workers");
  assert.equal(defaultTimeline.rule(worker).tone, "active");
  assert.equal(defaultTimeline.isFolded(mail.event_id, new Set()), true);
  assert.equal(defaultTimeline.isFolded(mail.event_id, new Set([mail.event_id])), false);
  const config = {
    ...DEFAULT_TIMELINE_CONFIG,
    turn: { lane: "human", glyph: "!", tone: "neutral", fold: "always" },
    mail: { lane: "children", glyph: "M", tone: "success", fold: "never" },
    statusTone: (item) => (item.status === "running" ? "warning" : undefined),
  };
  const custom = new EpisodeTimeline([mail, worker], config);
  assert.deepEqual(custom.lanes(), ["human", "children"]);
  assert.deepEqual(custom.rule(worker), {
    lane: "human",
    glyph: "!",
    tone: "warning",
    fold: "always",
  });
  assert.equal(custom.rule(mail).glyph, "M");
  assert.equal(custom.isFolded(mail.event_id, new Set([mail.event_id])), false);
  assert.equal(custom.isFolded(worker.event_id, new Set()), false);
  assert.equal(custom.isFolded(worker.event_id, new Set([worker.event_id])), true);
});

test("missing parents after truncation and malformed cycles remain visible", () => {
  const timeline = new EpisodeTimeline([
    event("mail:orphan", "mail", 0, "wake:dropped"),
    event("turn:a", "turn", 1, "turn:b"),
    event("turn:b", "turn", 2, "turn:a"),
    event("turn:self", "turn", 3, "turn:self"),
  ]);
  assert.equal(timeline.rows().length, 4);
  assert.equal(new EpisodeTimeline([]).rows().length, 0);
  assert.deepEqual(new EpisodeTimeline([]).lanes(), []);
});

test("timeline API uses the read-only project episode endpoint", async () => {
  const original = globalThis.fetch;
  let path;
  globalThis.fetch = async (url, init) => {
    path = url;
    assert.equal(init?.method ?? "GET", "GET");
    return new Response(
      JSON.stringify({
        episode_id: "episode/one",
        mode: "auto_research",
        events: [],
        truncated: false,
      }),
      { headers: { "Content-Type": "application/json" } },
    );
  };
  try {
    await fetchEpisodeTimeline("/api/projects/demo", "episode/one");
  } finally {
    globalThis.fetch = original;
  }
  assert.equal(path, "/api/projects/demo/episodes/episode%2Fone/timeline");
});

test("component renders configured lanes and folds, unknown provenance, and exact child links", async () => {
  const { createServer } = await import("vite");
  const React = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { middlewareMode: true },
  });
  try {
    const { EpisodeTimeline: Component } = await server.ssrLoadModule(
      "/src/components/EpisodeTimeline.tsx",
    );
    const mail = {
      ...event("mail:m", "mail", 1),
      detail: "A folded message",
      provenance: "unknown",
    };
    const child = {
      ...event("child:c:ended", "child", 2),
      links: { ...mail.links, episode_id: "child-episode", control_node_id: "experiment/one" },
    };
    const html = renderToStaticMarkup(
      React.createElement(Component, {
        events: [mail, child],
        apiBase: "/api/projects/demo",
        episodeId: "parent",
        graphTarget: { kind: "branch", branch_id: "parent" },
        onInspectTask() {},
        config: {
          ...DEFAULT_TIMELINE_CONFIG,
          mail: { lane: "human", glyph: "M", tone: "warning", fold: "folded" },
        },
      }),
    );
    assert.match(html, /data-lane="human"/);
    assert.match(html, /aria-expanded="false"/);
    assert.match(html, /provenance unknown/);
    assert.match(html, /A folded message/);
    assert.match(html, /episode=child-episode/);
    assert.match(html, /branch=parent/);
    assert.match(html, /parent=parent/);
    const workHtml = renderToStaticMarkup(
      React.createElement(Component, {
        events: [
          {
            ...child,
            event_id: "child:worker:admitted",
            links: { ...child.links, episode_id: "parent", task_id: "worker-turn" },
          },
        ],
        apiBase: "/api/projects/demo",
        episodeId: "parent",
        graphTarget: { kind: "branch", branch_id: "parent" },
        onInspectTask() {},
      }),
    );
    assert.match(workHtml, /<button type="button" class="episode-timeline-heading"/);
    assert.doesNotMatch(workHtml, /href=/);
  } finally {
    await server.close();
  }
});
