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
const model = await server.ssrLoadModule("/src/agents/experimentPlanModel.ts");
const { ExperimentPlanCardView } = await server.ssrLoadModule("/src/agents/ExperimentPlanCard.tsx");
const { approvalCardTitle } = await server.ssrLoadModule("/src/agents/agentThreadModel.ts");
const { ApiError } = await server.ssrLoadModule("/src/core/api.ts");

const action = (extra = {}) => ({
  kind: "experiment_start",
  project_id: "p",
  node_id: "exp/steering",
  branch_id: null,
  goal: "Pin model and seeds before compute",
  invocation_ceiling: 20,
  provider: "codex",
  model: "gpt-x",
  reasoning: "high",
  ...extra,
});
const card = (extra = {}) => ({
  kind: "experiment_start",
  action: action(),
  pinned: { node_id: "exp/steering" },
  pinned_sha256: "sha",
  expires_at: "2026-11-21T00:00:00Z",
  state: "pending",
  ...extra,
});
const question = (extra = {}) => ({
  question_id: "q1",
  operation_id: "op1",
  created_at: "2026-11-18T10:00:00Z",
  state: "pending",
  answer: null,
  can_answer: true,
  card: card(),
  ...extra,
});
const node = {
  id: "exp/steering",
  type: "experiment",
  title: "Shorter thinking with learned steering",
  updated_rev: 41,
  invocation_ceiling: 30,
  design: "Compare three arms on held-out states.",
  completion_criteria: ["All arms run", "Suites reported separately"],
};
const snapshot = {
  run_on: "m1",
  graph: { nodes: { [node.id]: node } },
  agent_profiles: {
    node_chat: {
      provider: "claude",
      model: "",
      reasoning: "",
      effective_model: "opus",
      work_like_capable: true,
    },
  },
  provider_readiness: {
    m1: {
      claude: { installed: true, authenticated: true, label: "Claude" },
      codex: { installed: true, authenticated: true, label: "Codex" },
    },
  },
};
const render = (extra = {}) =>
  renderToStaticMarkup(
    React.createElement(ExperimentPlanCardView, {
      question: question(),
      snapshot,
      staged: () => false,
      onPlanChanged: () => undefined,
      onSettled: () => undefined,
      ...extra,
    }),
  );
const startButton = (markup) => markup.match(/<button class="button primary"[^>]*>/)?.[0] ?? "";

test("the plan card shows its live node: title, chip, design and criteria", () => {
  const markup = render();
  for (const text of [node.title, node.id, node.design, ...node.completion_criteria])
    assert.ok(markup.includes(text), text);
  assert.ok(markup.includes(action().goal));
  assert.ok(approvalCardTitle(card()));
  assert.doesNotMatch(startButton(markup), /disabled/);
});

test("Start waits for Sync while the member has staged changes on the card's graph", () => {
  assert.match(startButton(render({ staged: () => true })), /disabled/);
});

test("Start waits for a live node, a goal, and a limit within the node's cap", () => {
  assert.match(
    startButton(render({ snapshot: { ...snapshot, graph: { nodes: {} } } })),
    /disabled/,
  );
  const inputs = model.initialPlanInputs(action(), null);
  assert.equal(model.planInputsReady(inputs, node, 30), true);
  assert.equal(model.planInputsReady({ ...inputs, goal: "  " }, node, 30), false);
  assert.equal(model.planInputsReady({ ...inputs, invocationCeiling: 31 }, node, 30), false);
  assert.equal(model.planInputsReady({ ...inputs, invocationCeiling: 0 }, node, 30), false);
  assert.equal(model.planInputsReady({ ...inputs, invocationCeiling: 2.5 }, node, 30), false);
  assert.equal(model.planInputsReady(inputs, null, 30), false);
  assert.equal(model.planTurnCap(action(), node), 30);
  assert.equal(model.planTurnCap(action(), null), 20);
});

test("a settled plan card is one receipt line without its body or buttons", () => {
  const settled = render({
    question: question({
      state: "answered",
      answer: "approved",
      card: card({ result: { outcome: "approved" } }),
    }),
  });
  assert.ok(settled.includes(node.id));
  assert.ok(!settled.includes(node.design));
  assert.equal(startButton(settled), "");
});

test("the agent's pick seeds the inputs; without one the profile does", () => {
  const fallback = { provider: "claude", model: "", reasoning: "", run_on: "m1" };
  assert.deepEqual(model.initialPlanInputs(action(), fallback).config, {
    provider: "codex",
    model: "gpt-x",
    reasoning: "high",
    run_on: "m1",
  });
  assert.deepEqual(
    model.initialPlanInputs(action({ provider: null, model: null, reasoning: null }), fallback)
      .config,
    fallback,
  );
});

test("Start sends the human's inputs and the node revision the card showed", () => {
  const inputs = {
    goal: "  Edited goal  ",
    invocationCeiling: 12,
    config: { provider: "claude", model: "", reasoning: "low", run_on: "m1" },
  };
  assert.deepEqual(model.planStartDecision(card(), inputs, node), {
    decision: "approve",
    pinned_sha256: "sha",
    experiment_start: {
      goal: "Edited goal",
      invocation_ceiling: 12,
      provider: "claude",
      model: null,
      reasoning: "low",
      node_rev: 41,
    },
  });
});

test("a moved plan keeps the card pending; a taken card settles from the refresh", async () => {
  const sent = [];
  const answer = (respond) => async (questionId, decision) => {
    sent.push({ questionId, decision });
    return respond();
  };
  const refuse = (status, detail) => () => {
    throw new ApiError(detail.message ?? JSON.stringify(detail), status, detail);
  };
  const decision = model.planStartDecision(card(), model.initialPlanInputs(action(), null), node);
  const moved = await model.tapPlanCard(
    "q1",
    decision,
    answer(refuse(409, { code: "plan_changed" })),
  );
  assert.equal(moved.kind, "pending");
  assert.equal(moved.planChanged, true);
  assert.ok(moved.reason);
  assert.ok(!moved.reason.includes("plan_changed"));
  const said = await model.tapPlanCard(
    "q1",
    decision,
    answer(refuse(409, { code: "plan_changed", message: "Read it again" })),
  );
  assert.equal(said.reason, "Read it again");
  const refused = await model.tapPlanCard(
    "q1",
    decision,
    answer(refuse(422, { code: "limit", message: "Too many turns" })),
  );
  assert.deepEqual(refused, { kind: "pending", reason: "Too many turns", planChanged: false });
  assert.deepEqual(
    await model.tapPlanCard("q1", decision, answer(refuse(409, { code: "card_taken" }))),
    { kind: "settled", result: null },
  );
  const result = { outcome: "approved", episode_id: "ep" };
  assert.deepEqual(
    await model.tapPlanCard(
      "q1",
      decision,
      answer(() => result),
    ),
    { kind: "settled", result },
  );
  assert.deepEqual(sent.at(-1), { questionId: "q1", decision });
});

test("only experiment_start cards are plan cards, and a branch card targets its branch", () => {
  assert.equal(model.isPlanCardQuestion(question()), true);
  assert.equal(
    model.isPlanCardQuestion(question({ card: { ...card(), action: { kind: "merge_branch" } } })),
    false,
  );
  assert.equal(model.isPlanCardQuestion(question({ card: null })), false);
  assert.deepEqual(model.planGraphTarget(action()), { kind: "main" });
  assert.deepEqual(model.planGraphTarget(action({ branch_id: "b1" })), {
    kind: "branch",
    branch_id: "b1",
  });
});

const { hasStagedHumanDraft, persistProjectHumanDraft } = await server.ssrLoadModule(
  "/src/graph/projectSession.ts",
);
const { emptyHumanDraft } = await server.ssrLoadModule("/src/graph/humanDraft.ts");
const { requestNodeEdit, takeNodeEditRequest } = await server.ssrLoadModule(
  "/src/graph/nodeEditRequest.ts",
);

test("staged changes count only for this member, project and graph target", () => {
  const items = new Map();
  const storage = {
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => items.set(key, value),
    removeItem: (key) => items.delete(key),
  };
  const branch = { kind: "branch", branch_id: "b1" };
  const draft = { ...emptyHumanDraft(1), removed_node_ids: ["n"] };
  persistProjectHumanDraft(storage, "u1", "p", draft, branch);
  assert.equal(hasStagedHumanDraft(storage, "u1", "p", branch), true);
  assert.equal(hasStagedHumanDraft(storage, "u1", "p", { kind: "main" }), false);
  assert.equal(hasStagedHumanDraft(storage, "u2", "p", branch), false);
  persistProjectHumanDraft(storage, "u1", "p", emptyHumanDraft(1), branch);
  assert.equal(hasStagedHumanDraft(storage, "u1", "p", branch), false);
  const broken = {
    getItem: () => {
      throw new Error("blocked");
    },
  };
  assert.equal(hasStagedHumanDraft(broken, "u1", "p", branch), false);
});

test("an Edit node request is taken once, by the drawer showing that exact node", () => {
  const main = { kind: "main" };
  requestNodeEdit({ projectId: "p", graphTarget: main, nodeId: "exp/a" });
  assert.equal(takeNodeEditRequest("p", { kind: "branch", branch_id: "b" }, "exp/a"), false);
  assert.equal(takeNodeEditRequest("p", main, "exp/b"), false);
  assert.equal(takeNodeEditRequest("p", main, "exp/a"), true);
  assert.equal(takeNodeEditRequest("p", main, "exp/a"), false);
});
