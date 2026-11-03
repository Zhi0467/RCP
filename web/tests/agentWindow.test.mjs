import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const { agentWindowEntries, AgentWindowTranscript, lineTapShowsTime, timeDividers } =
  await server.ssrLoadModule("/src/agentWindow/AgentWindowTranscript.tsx");
const { localClock, timeDividerLabel } = await server.ssrLoadModule(
  "/src/agents/agentThreadModel.ts",
);
const { reconstructTaskTranscript } = await server.ssrLoadModule("/src/agents/agentTasks.ts");
const { projectAgentWindowEndpoints, spaceAgentWindowEndpoints } = await server.ssrLoadModule(
  "/src/agentWindow/agentWindowEndpoints.ts",
);
const { useAgentWindowLines } = await server.ssrLoadModule(
  "/src/agentWindow/useAgentWindowLines.ts",
);
const { chatMessageTranscriptLine } = await server.ssrLoadModule("/src/agents/agentTasks.ts");

const line = (id, role, taskId, timestamp, extra = {}) => ({
  lineId: id,
  role,
  taskId,
  timestamp: `2026-11-01T10:00:${timestamp}Z`,
  text: id,
  ...extra,
});
const card = (extra = {}) => ({
  question_id: "q1",
  operation_id: "op1",
  created_at: "2026-11-01T10:00:05Z",
  state: "pending",
  card: { state: "pending" },
  ...extra,
});
const proposalCard = (id, turnId, extra = {}) => ({
  project_id: "p",
  project_name: "Project",
  thread_id: "chat",
  turn_id: turnId,
  turn_created_at: "2026-11-01T10:00:00Z",
  proposal: { id, title: id, status: "pending" },
  stale: false,
  changes: [],
  ...extra,
});
const keys = (entries) =>
  entries.flatMap((entry) =>
    entry.kind === "line"
      ? [entry.line.lineId]
      : entry.kind === "question"
        ? [entry.question.question_id]
        : [
            ...entry.proposals.map((card) => card.proposal.id),
            ...entry.asked.map((question) => question.question_id),
          ],
  );
const groups = (entries) =>
  entries
    .filter((entry) => entry.kind === "cards")
    .map((entry) => [
      ...entry.proposals.map((card) => card.proposal.id),
      ...entry.asked.map((question) => question.question_id),
    ]);

test("approval stays after its request and before its answer when both records use settlement time", () => {
  const lines = [
    line("request", "human", "op1", "10"),
    line("answer", "agent", "op1", "10"),
    line("later", "human", "op2", "20"),
  ];
  assert.deepEqual(keys(agentWindowEntries(lines, [card()])), ["request", "q1", "answer", "later"]);
  assert.deepEqual(
    keys(agentWindowEntries(lines, [card({ state: "answered", card: { state: "approved" } })])),
    ["request", "q1", "answer", "later"],
  );
});
test("only a typed delivery for an approval card is hidden, regardless of answer text", () => {
  const lines = [
    line("request", "human", "op1", "00"),
    line("delivery", "human", "op2", "20", { questionId: "q1", text: "opaque action result" }),
    line("same-text", "human", "op3", "30", { text: "opaque action result" }),
    line("plain-question", "human", "op4", "40", { questionId: "plain" }),
  ];
  assert.deepEqual(keys(agentWindowEntries(lines, [card()])), [
    "request",
    "q1",
    "same-text",
    "plain-question",
  ]);
});
test("running cards and reconstructed replies use their owning turn, while ordinary open questions remain footer controls", () => {
  const lines = [
    line("request", "human", "op1", "00"),
    line("answer", "agent", "op1", "00"),
    line("later", "human", "op2", "20"),
  ];
  assert.deepEqual(
    keys(agentWindowEntries(lines, [card(), card({ question_id: "plain", card: null })])),
    ["request", "q1", "answer", "later"],
  );
});
test("project and Space endpoints keep capabilities and encoded owner routes separate", () => {
  const project = projectAgentWindowEndpoints("project/a", "chat/b");
  const space = spaceAgentWindowEndpoints({
    thread_id: "thread/a",
    kind: "space",
    project_id: null,
  });
  assert.equal(
    project.artifactUrl("op/a", "id/a", "viewer"),
    "/api/projects/project%2Fa/tasks/op%2Fa/artifacts/id%2Fa/viewer",
  );
  assert.equal(project.attachmentsUrl, "/api/projects/project%2Fa/chats/chat%2Fb/attachments");
  assert.equal(
    space.artifactUrl("op/a", "id/a", "viewer"),
    "/api/agent-threads/thread%2Fa/tasks/op%2Fa/artifacts/id%2Fa/viewer",
  );
  assert.equal(space.attachmentsUrl, "/api/agent-threads/thread%2Fa/attachments");
  assert.equal(space.scopeKey, "space:thread/a");
  assert.equal(space.artifactState, null);
  assert.equal(space.openArtifactPanel, null);
  assert.equal(space.canKeep, false);
  assert.equal(space.canCommentArtifacts, false);
  assert.equal(project.canKeep, true);
  assert.equal(project.canCommentArtifacts, true);
});
test("transcript projection preserves voice, schedule and question identity", () => {
  const projected = chatMessageTranscriptLine({
    message_id: "m",
    role: "user",
    timestamp: "now",
    source: "voice",
    question_id: "q",
    trigger: "schedule",
    text: "voice",
  });
  assert.equal(projected.source, "voice");
  assert.equal(projected.trigger, "schedule");
  assert.equal(projected.questionId, "q");
});
test("the shared lines hook appends one pending turn, but not a duplicate store overlay", () => {
  const pending = {
    clientId: "pending",
    text: "hello",
    timestamp: "2026-11-01T00:00:00Z",
    mode: "work",
    attachments: [],
  };
  let result;
  function Probe({ messages }) {
    result = useAgentWindowLines({ messages, tasks: [], pendingTurn: pending });
    return null;
  }
  renderToStaticMarkup(React.createElement(Probe, { messages: [] }));
  assert.equal(result.transcript.length, 1);
  renderToStaticMarkup(
    React.createElement(Probe, {
      messages: [
        {
          message_id: "m",
          operation_id: "pending",
          role: "user",
          text: "hello",
          timestamp: pending.timestamp,
        },
      ],
    }),
  );
  assert.equal(result.transcript.length, 1);
  assert.equal(result.transcript[0].lineId, "message:m");
});

test("multiple pending cards stay in order before an answer exists", () => {
  assert.deepEqual(
    keys(
      agentWindowEntries(
        [line("request", "human", "op1", "00")],
        [card(), card({ question_id: "q2", created_at: "2026-11-01T10:00:06Z" })],
      ),
    ),
    ["request", "q1", "q2"],
  );
});

test("task-only approval followups preserve the marker before history catches up", () => {
  let result;
  function Probe() {
    result = useAgentWindowLines({
      messages: [],
      tasks: [
        {
          operation_id: "followup",
          question_id: "q1",
          created_at: "2026-11-01T10:00:20Z",
          request: { message: "opaque delivery", mode: "work" },
          active: true,
          result: null,
        },
      ],
    });
    return null;
  }
  renderToStaticMarkup(React.createElement(Probe));
  assert.equal(result.transcript[0].questionId, "q1");
  assert.deepEqual(keys(agentWindowEntries(result.transcript, [card()])), ["q1"]);
});

test("human lead renders inside the bubble while turn markers stay outside", () => {
  const markup = renderToStaticMarkup(
    React.createElement(AgentWindowTranscript, {
      transcript: [line("spoken", "human", "op1", "00", { source: "voice" })],
      tasks: [],
      questions: [],
      endpoints: projectAgentWindowEndpoints("p", "chat"),
      artifacts: { renderArtifacts: () => null },
      comments: {},
      compactionByLine: new Map(),
      continuedTaskIds: new Set(),
      latestGraphReceiptLineIds: new Set(),
      lineLead: () => React.createElement("i", { "data-turn-marker": true }),
      humanLead: () => React.createElement("i", { "data-source-icon": true }),
    }),
  );
  assert.match(markup, /data-turn-marker="true"[^]*class="chat-human-message"/);
  assert.match(
    markup,
    /class="chat-human-message"><i data-source-icon="true"><\/i><span class="node-chat-text">spoken<\/span><\/div>/,
  );
});

test("project task adapters preserve encoded routes and control idempotency", async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ operation_id: "op/a" }));
  };
  try {
    const endpoints = projectAgentWindowEndpoints("project/a", "chat/b");
    assert.equal((await endpoints.loadTask("op/a")).operation_id, "op/a");
    await endpoints.control("op/a", "pause", "request-1");
    await endpoints.control("op/a", "resume", "request-2");
    await endpoints.control("op/a", "retry", "request-3");
    assert.deepEqual(
      calls.map(({ url }) => url),
      [
        "/api/projects/project%2Fa/tasks/op%2Fa",
        "/api/projects/project%2Fa/tasks/op%2Fa/pause",
        "/api/projects/project%2Fa/tasks/op%2Fa/resume",
        "/api/projects/project%2Fa/tasks/op%2Fa/retry",
      ],
    );
    assert.equal(calls[1].init.method, "POST");
    assert.deepEqual(
      calls.slice(1).map(({ init }) => new Headers(init.headers).get("Idempotency-Key")),
      [null, "request-2", "request-3"],
    );
  } finally {
    globalThis.fetch = original;
  }
});

const at = (...parts) => new Date(...parts).toISOString();
const timed = (id, shownAt, extra = {}) => ({
  lineId: id,
  role: "human",
  taskId: id,
  timestamp: "2026-11-01T00:00:00Z",
  shownAt,
  text: id,
  ...extra,
});
const dividerIds = (lines, questions = []) => [
  ...timeDividers(agentWindowEntries(lines, questions)).keys(),
];

test("a divider opens the thread, then marks a 30 minute gap but not 29", () => {
  const lines = [
    timed("first", at(2026, 10, 2, 9, 0)),
    timed("after-29", at(2026, 10, 2, 9, 29)),
    timed("after-30", at(2026, 10, 2, 9, 59)),
  ];
  assert.deepEqual(dividerIds(lines), ["first", "after-30"]);
});

test("a divider marks a new local day even after a short gap", () => {
  const lines = [timed("late", at(2026, 10, 2, 23, 50)), timed("early", at(2026, 10, 3, 0, 5))];
  assert.deepEqual(dividerIds(lines), ["late", "early"]);
});

test("receipt-only lines, running replies and cards are not counted for dividers", () => {
  const graphUpdate = { status: "applied", proposal_ids: [], change_summary: [] };
  const lines = [
    timed("receipt", at(2026, 10, 2, 8, 0), { role: "agent", text: "", graphUpdate }),
    timed("first", at(2026, 10, 2, 9, 0)),
    timed("running", null, { role: "agent", text: "", running: true }),
    timed("receipt-later", at(2026, 10, 2, 9, 40), { role: "agent", text: "", graphUpdate }),
    timed("next", at(2026, 10, 2, 9, 20)),
  ];
  const question = card({ operation_id: "first", created_at: at(2026, 10, 2, 11, 0) });
  assert.deepEqual(dividerIds(lines, [question]), ["first"]);
});

test("a line without its own shown time falls back to its timestamp", () => {
  const lines = [
    timed("first", at(2026, 10, 2, 9, 0)),
    { ...timed("pending", undefined), timestamp: at(2026, 10, 2, 10, 0) },
  ];
  assert.deepEqual(dividerIds(lines), ["first", "pending"]);
});

test("divider labels name today, yesterday, a weekday date, and the year when it differs", () => {
  const now = new Date(2026, 10, 2, 20, 0);
  assert.equal(timeDividerLabel(new Date(2026, 10, 2, 19, 21), now), "Today · 19:21");
  assert.equal(timeDividerLabel(new Date(2026, 10, 1, 8, 2), now), "Yesterday · 08:02");
  assert.equal(timeDividerLabel(new Date(2026, 9, 26, 19, 21), now), "Mon 26 Oct · 19:21");
  assert.equal(timeDividerLabel(new Date(2025, 10, 2, 19, 21), now), "Sun 2 Nov 2025 · 19:21");
  assert.equal(
    timeDividerLabel(new Date(2026, 0, 31, 7, 5), new Date(2026, 1, 1, 0, 1)),
    "Yesterday · 07:05",
  );
  assert.equal(localClock(new Date(2026, 10, 2, 0, 7)), "00:07");
});

test("a tap shows the time unless it lands on a control or selects text", () => {
  const target = (matches) => ({ closest: (selector) => (matches(selector) ? {} : null) });
  const plain = target(() => false);
  const onLink = target((selector) => selector.includes("a,") || selector.startsWith("a"));
  const collapsed = { isCollapsed: true, toString: () => "" };
  const selecting = { isCollapsed: false, toString: () => "some words" };
  assert.equal(lineTapShowsTime(plain, collapsed), true);
  assert.equal(lineTapShowsTime(plain, null), true);
  assert.equal(lineTapShowsTime(onLink, collapsed), false);
  assert.equal(lineTapShowsTime(plain, selecting), false);
});

test("the transcript renders each divider once and a time on every timed line but a wake", () => {
  const markup = renderToStaticMarkup(
    React.createElement(AgentWindowTranscript, {
      transcript: [
        timed("first", at(2026, 10, 2, 9, 0)),
        timed("soon", at(2026, 10, 2, 9, 5)),
        timed("wake", at(2026, 10, 2, 9, 7), { trigger: "schedule" }),
        timed("running", null, { role: "agent", text: "", running: true }),
      ],
      tasks: [],
      questions: [],
      endpoints: projectAgentWindowEndpoints("p", "chat"),
      artifacts: { renderArtifacts: () => null },
      comments: {},
      compactionByLine: new Map(),
      continuedTaskIds: new Set(),
      latestGraphReceiptLineIds: new Set(),
    }),
  );
  assert.equal(markup.match(/class="chat-time-divider"/g)?.length, 1);
  assert.equal(markup.match(/<time class="chat-line-time"/g)?.length, 2);
  assert.ok(markup.indexOf("chat-time-divider") < markup.indexOf('data-task-id="first"'));
});

test("reconstructed replies show when they finished and nothing while running", () => {
  const task = (extra) => ({
    operation_id: "op",
    created_at: "2026-11-02T09:00:00Z",
    request: { message: "go", mode: "work" },
    result: { messages: ["done"] },
    active: false,
    ...extra,
  });
  const shown = (lines) => lines.map((item) => [item.role, item.shownAt]);
  assert.deepEqual(
    shown(reconstructTaskTranscript([task({ finished_at: "2026-11-02T09:04:00Z" })])),
    [
      ["human", "2026-11-02T09:00:00Z"],
      ["agent", "2026-11-02T09:04:00Z"],
    ],
  );
  assert.deepEqual(shown(reconstructTaskTranscript([task({ finished_at: null })])), [
    ["human", "2026-11-02T09:00:00Z"],
    ["agent", null],
  ]);
  assert.deepEqual(
    shown(reconstructTaskTranscript([task({ result: null, error: "boom", finished_at: null })])),
    [
      ["human", "2026-11-02T09:00:00Z"],
      ["error", "2026-11-02T09:00:00Z"],
    ],
  );
  assert.equal(
    chatMessageTranscriptLine({ message_id: "m", role: "user", timestamp: "t", text: "x" }).shownAt,
    "t",
  );
  assert.equal(
    chatMessageTranscriptLine({
      message_id: "m",
      role: "user",
      timestamp: "t",
      shown_at: "s",
      text: "x",
    }).shownAt,
    "s",
  );
});

test("Proposal cards sit in their raising turn before its answer, grouped with that turn's asked cards", () => {
  const lines = [
    line("request", "human", "op1", "00"),
    line("answer", "agent", "op1", "30"),
    line("later", "human", "op2", "40"),
  ];
  const entries = agentWindowEntries(lines, [card()], [proposalCard("pr-1", "op1")]);
  assert.deepEqual(keys(entries), ["request", "pr-1", "q1", "answer", "later"]);
  assert.deepEqual(groups(entries), [["pr-1", "q1"]]);
});

test("a Proposal card of a running retry follows its first attempt's lines", () => {
  const lines = [
    line("request", "human", "op1", "00"),
    line("retry-live", "agent", "op1-retry", "10", { running: true }),
    line("other", "human", "op2", "40"),
  ];
  assert.deepEqual(keys(agentWindowEntries(lines, [], [proposalCard("pr-1", "op1")])), [
    "request",
    "pr-1",
    "retry-live",
    "other",
  ]);
});

test("a Proposal card of a turn with no lines takes its time position, else the end", () => {
  const lines = [line("early", "human", "op0", "00"), line("late", "human", "op2", "40")];
  assert.deepEqual(
    keys(
      agentWindowEntries(
        lines,
        [],
        [proposalCard("pr-1", "gone", { turn_created_at: "2026-11-01T10:00:20Z" })],
      ),
    ),
    ["early", "pr-1", "late"],
  );
  assert.deepEqual(
    keys(
      agentWindowEntries(
        lines,
        [],
        [proposalCard("pr-2", "gone", { turn_created_at: "2026-11-01T10:01:00Z" })],
      ),
    ),
    ["early", "late", "pr-2"],
  );
});

test("cards at different turns form separate entries", () => {
  const lines = [
    line("a", "human", "op1", "00"),
    line("a-answer", "agent", "op1", "10"),
    line("b", "human", "op2", "20"),
    line("b-answer", "agent", "op2", "30"),
  ];
  const entries = agentWindowEntries(
    lines,
    [],
    [
      proposalCard("pr-1", "op1"),
      proposalCard("pr-2", "op1"),
      proposalCard("pr-3", "op2", { turn_created_at: "2026-11-01T10:00:20Z" }),
    ],
  );
  assert.deepEqual(groups(entries), [["pr-1", "pr-2"], ["pr-3"]]);
  const stacks = entries.filter((entry) => entry.kind === "cards");
  assert.equal(new Set(stacks.map((entry) => entry.key)).size, stacks.length);
});

test("a card group keeps its key when an earlier group of the same turn empties", () => {
  const lines = [
    line("request", "human", "op1", "00"),
    line("first", "agent", "op1", "10"),
    line("second", "agent", "op1", "20"),
  ];
  const early = card();
  const late = card({ question_id: "q2", created_at: "2026-11-01T10:00:15Z" });
  const stackKeys = (questions) =>
    agentWindowEntries(lines, questions)
      .filter((entry) => entry.kind === "cards")
      .map((entry) => entry.key);
  const both = stackKeys([early, late]);
  assert.equal(new Set(both).size, 2);
  assert.deepEqual(stackKeys([late]), [both[1]]);
});

test("without renderCards each asked card renders through renderQuestion and Proposal cards add nothing", () => {
  const props = (extra) => ({
    transcript: [line("request", "human", "op1", "00"), line("answer", "agent", "op1", "30")],
    tasks: [],
    questions: [card(), card({ question_id: "q2", created_at: "2026-11-01T10:00:06Z" })],
    endpoints: projectAgentWindowEndpoints("p", "chat"),
    artifacts: { renderArtifacts: () => null, repositoryFileErrors: new Map() },
    comments: {},
    compactionByLine: new Map(),
    continuedTaskIds: new Set(),
    latestGraphReceiptLineIds: new Set(),
    readOnly: true,
    renderQuestion: (question) =>
      React.createElement("i", { "data-question": question.question_id }),
    ...extra,
  });
  const plain = renderToStaticMarkup(React.createElement(AgentWindowTranscript, props({})));
  assert.match(plain, /<i data-question="q1"><\/i><i data-question="q2"><\/i>/);
  assert.equal(
    renderToStaticMarkup(
      React.createElement(
        AgentWindowTranscript,
        props({ proposalCards: [proposalCard("pr-1", "op1")] }),
      ),
    ),
    plain,
  );
  const stacked = renderToStaticMarkup(
    React.createElement(
      AgentWindowTranscript,
      props({
        proposalCards: [proposalCard("pr-1", "op1")],
        renderCards: (entry) =>
          React.createElement("b", {
            "data-stack": [
              ...entry.proposals.map((item) => item.proposal.id),
              ...entry.asked.map((item) => item.question_id),
            ].join(","),
          }),
      }),
    ),
  );
  assert.match(stacked, /<b data-stack="pr-1,q1,q2"><\/b>/);
  assert.doesNotMatch(stacked, /data-question/);
});
