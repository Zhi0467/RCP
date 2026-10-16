import assert from "node:assert/strict";
import test from "node:test";

import {
  agentContextTokens,
  agentContextWindow,
  agentStatusTask,
  agentStatusView,
  chatCompactions,
  compactionAnchors,
  contextFraction,
  formatTokens,
} from "../src/chat/agentStatusModel.ts";

function status(overrides = {}) {
  return {
    context_tokens: null,
    context_window: null,
    autocompact_tokens: null,
    compacting: false,
    compactions: [],
    subagents: [],
    ...overrides,
  };
}

function task(id, overrides = {}) {
  return {
    operation_id: id,
    active: false,
    finished: true,
    request: { provider: "claude", model: "opus" },
    agent_status: null,
    ...overrides,
  };
}

test("the active turn wins, else the latest finished turn", () => {
  const done = task("a");
  const running = task("b", { active: true, finished: false });
  const later = task("c");
  assert.equal(agentStatusTask([done, running, later]).operation_id, "b");
  assert.equal(agentStatusTask([done, later]).operation_id, "c");
  const paused = task("p", { finished: false });
  assert.equal(agentStatusTask([done, paused]).operation_id, "p");
});

test("a window falls back to an earlier turn of the same provider and model only", () => {
  const learned = task("a", { agent_status: status({ context_window: 200_000 }) });
  const otherModel = task("b", {
    request: { provider: "claude", model: "sonnet" },
    agent_status: status({ context_window: 1_000_000 }),
  });
  const current = task("c", {
    active: true,
    finished: false,
    agent_status: status({ context_tokens: 50_000 }),
  });
  assert.equal(agentContextWindow(current, [learned, otherModel, current]), 200_000);
  assert.equal(agentContextWindow(current, [otherModel, current]), null);
  // A later turn never lends its window to an earlier one.
  assert.equal(agentContextWindow(learned, [learned, current]), 200_000);
  const own = task("d", { agent_status: status({ context_window: 400_000 }) });
  assert.equal(agentContextWindow(own, [learned, own]), 400_000);
});

test("a resumed turn shows its session's last context until it reports one", () => {
  const previous = task("a", {
    native_session_id: "s1",
    agent_status: status({ context_tokens: 36_000 }),
  });
  const resumed = task("b", { active: true, finished: false, request: { session_id: "s1" } });
  const fallback = task("e", {
    active: true,
    finished: false,
    runtime_id: "codex.exec-json.v1",
    request: { session_id: "s1" },
  });
  assert.equal(agentContextTokens(fallback, [previous, fallback]), null);
  const fresh = task("c", { active: true, finished: false, request: {} });
  assert.equal(agentContextTokens(resumed, [previous, resumed]), 36_000);
  assert.equal(agentContextTokens(fresh, [previous, fresh]), null);
  const reported = task("d", {
    request: { session_id: "s1" },
    agent_status: status({ context_tokens: 38_000 }),
  });
  assert.equal(agentContextTokens(reported, [previous, reported]), 38_000);
});

test("view counts running subagents and gathers compactions across turns", () => {
  const first = task("a", {
    agent_status: status({
      compactions: [{ trigger: "auto", before_tokens: 182_000, after_tokens: 31_000 }],
    }),
  });
  const current = task("b", {
    active: true,
    finished: false,
    agent_status: status({
      context_tokens: 119_000,
      context_window: 200_000,
      compactions: [{ trigger: "manual", before_tokens: null, after_tokens: 20_000 }],
      subagents: [
        { id: "1", kind: "Explore", label: "", state: "running", tokens: null },
        { id: "2", kind: "Explore", label: "", state: "completed", tokens: 29_000 },
        { id: "3", kind: "general", label: "", state: "running", tokens: null },
      ],
    }),
  });
  const view = agentStatusView([first, current]);
  assert.equal(view.task.operation_id, "b");
  assert.equal(view.runningSubagents, 2);
  assert.deepEqual(
    chatCompactions([first, current]).map((c) => [c.operationId, c.turn]),
    [
      ["a", 1],
      ["b", 2],
    ],
  );
  assert.equal(contextFraction(view), 0.595);
});

test("a runtime without context reports no fraction", () => {
  const view = agentStatusView([task("a", { agent_status: status() })]);
  assert.equal(view.contextTokens, null);
  assert.equal(contextFraction(view), null);
  const windowless = agentStatusView([task("b", { agent_status: status({ context_tokens: 10 }) })]);
  assert.equal(contextFraction(windowless), null);
});

test("token counts scale to k and M", () => {
  assert.deepEqual([850, 119_400, 200_000, 1_000_000, 1_200_000].map(formatTokens), [
    "850",
    "119k",
    "200k",
    "1M",
    "1.2M",
  ]);
});

test("a compaction divider anchors before the reply, or after a lone human line", () => {
  const compactions = [{ trigger: "auto", before_tokens: 1, after_tokens: 0 }];
  const finished = task("a", { agent_status: status({ compactions }) });
  const running = task("b", { active: true, agent_status: status({ compactions }) });
  const quiet = task("c");
  const lines = [
    { lineId: "a:h", role: "human", taskId: "a" },
    { lineId: "a:r", role: "agent", taskId: "a" },
    { lineId: "b:h", role: "human", taskId: "b" },
    { lineId: "c:h", role: "human", taskId: "c" },
  ];
  const anchors = compactionAnchors(lines, [finished, running, quiet]);
  assert.deepEqual(
    [...anchors].map(([id, anchor]) => [id, anchor.placement]),
    [
      ["a:r", "before"],
      ["b:h", "after"],
    ],
  );
});
