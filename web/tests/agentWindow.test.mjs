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
const { agentWindowEntries, AgentWindowTranscript } = await server.ssrLoadModule(
  "/src/agentWindow/AgentWindowTranscript.tsx",
);
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
const keys = (entries) =>
  entries.map((entry) => (entry.kind === "line" ? entry.line.lineId : entry.question.question_id));

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
    assert.deepEqual(
      calls.map(({ url }) => url),
      ["/api/projects/project%2Fa/tasks/op%2Fa", "/api/projects/project%2Fa/tasks/op%2Fa/pause"],
    );
    assert.equal(calls[1].init.method, "POST");
    assert.equal(new Headers(calls[1].init.headers).get("Idempotency-Key"), "request-1");
  } finally {
    globalThis.fetch = original;
  }
});
