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
const { EpisodePlanCardView: ExperimentPlanCardView } = await server.ssrLoadModule(
  "/src/agents/EpisodePlanCard.tsx",
);
const { approvalCardTitle } = await server.ssrLoadModule("/src/agents/agentThreadModel.ts");
const { ApiError } = await server.ssrLoadModule("/src/core/api.ts");
const taskCards = await server.ssrLoadModule("/src/agents/taskCardModel.ts");
const { hasStagedHumanDraft, persistProjectHumanDraft } = await server.ssrLoadModule(
  "/src/graph/projectSession.ts",
);
const { emptyHumanDraft } = await server.ssrLoadModule("/src/graph/humanDraft.ts");

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

test("a card reads its phase from the server's card state, and a run's display wins", () => {
  const phase = (extra, run) => {
    const view = taskCards.taskCardView(card(extra), run);
    return [view.phase, view.open, view.state];
  };
  assert.deepEqual(phase({}), ["needs_you", true, "pending"]);
  assert.deepEqual(phase({ state: "parked" }), ["parked", true, "pending"]);
  assert.deepEqual(phase({ state: "running" }), ["starting", false, "pending"]);
  assert.deepEqual(phase({ state: "running_here" }), ["running_here", false, "pending"]);
  assert.deepEqual(phase({ state: "ran_here", result: { outcome: "run_here" } }), [
    "ran_here",
    false,
    "run_here",
  ]);
  assert.deepEqual(phase({ state: "expired" }), ["settled", false, "expired"]);
  assert.deepEqual(phase({ state: "parked", result: { outcome: "declined" } }), [
    "settled",
    false,
    "declined",
  ]);
  const run = {
    phase: "ended",
    state_line: "Completed · 9 turns",
    tone: "completed",
    section: "ended",
  };
  const shown = taskCards.taskCardView(card({ state: "running" }), run);
  assert.deepEqual(
    [shown.phase, shown.label, shown.tone, shown.open],
    ["ended", run.state_line, run.tone, false],
  );
  assert.equal(
    taskCards.taskCardView(undefined, { ...run, phase: "needs_you" }).phase,
    "needs_you",
  );
});

test("a card joins its run by the episode ids it names and renders in its revising turn", () => {
  const runs = ["episode/a", "episode/b"].map((episode_id) => ({ episode: { episode_id } }));
  const startedCard = card({ started_episode_id: "episode/a", current_episode_id: "episode/b" });
  assert.equal(taskCards.cardRun(startedCard, runs), runs[1]);
  assert.equal(taskCards.cardRun(card(), runs), null);
  assert.equal(taskCards.cardAwaitsRun(startedCard, true), true);
  assert.equal(taskCards.cardAwaitsRun(startedCard, false), false);
  assert.equal(taskCards.cardAwaitsRun(card(), true), false);
  const asked = question({ card: startedCard });
  assert.equal(taskCards.runCard(runs[0], [asked]), asked);
  assert.equal(taskCards.runCard({ episode: { episode_id: "episode/c" } }, [asked]), null);
  assert.equal(taskCards.cardTurnId(question()), "op1");
  assert.equal(
    taskCards.cardTurnId(question({ card: card({ revised_operation_id: "op2" }) })),
    "op2",
  );
});

test("the pane lists runs by their published section and parked plans newest first", () => {
  const run = (episode_id, section) => ({ episode: { episode_id, presentation: { section } } });
  const parked = (question_id, shelved_at) =>
    question({ question_id, card: card({ state: "parked", shelved_at }) });
  const content = taskCards.agentPaneContent(
    [run("a", "running"), run("b", "ended"), run("c", "running")],
    [
      parked("old", "2026-11-18T10:00:00Z"),
      question(),
      parked("new", "2026-11-19T10:00:00Z"),
      parked("gone", "2026-11-20T10:00:00Z"),
    ].map((item) =>
      item.question_id === "gone"
        ? { ...item, card: { ...item.card, result: { outcome: "declined" } } }
        : item,
    ),
    [{ artifact_id: "x" }],
  );
  assert.deepEqual(
    content.running.map((item) => item.episode.episode_id),
    ["a", "c"],
  );
  assert.deepEqual(
    content.ended.map((item) => item.episode.episode_id),
    ["b"],
  );
  assert.deepEqual(
    content.parked.map((item) => item.question_id),
    ["new", "old"],
  );
  assert.equal(taskCards.agentPaneCount(content), 5);
});

test("a node edit Syncs from the draft's revision and a moved node is a conflict", async () => {
  const save = {
    projectId: "p",
    target: { kind: "main" },
    nodeId: node.id,
    baseUpdatedRev: 41,
    changes: { design: "New" },
  };
  const graphAt = (revision, updated_rev) => ({
    revision,
    nodes: { [node.id]: { ...node, updated_rev } },
  });
  const sent = [];
  const refusal = new ApiError("moved", 409);
  const ports = (reads, answers) => ({
    read: async () => reads.shift(),
    sync: async (_project, _target, body) => {
      sent.push(body);
      const answer = answers.shift();
      if (answer instanceof Error) throw answer;
      return answer;
    },
  });
  assert.deepEqual(await model.savePlanNodeEdit(save, ports([graphAt(7, 41)], [graphAt(8, 42)])), {
    kind: "saved",
    nodeRev: 42,
  });
  assert.deepEqual(sent.pop(), {
    base_revision: 7,
    nodes: [{ node_id: node.id, base_updated_rev: 41, changes: { design: "New" } }],
  });
  assert.deepEqual(
    await model.savePlanNodeEdit(
      save,
      ports([graphAt(7, 41), graphAt(9, 41)], [refusal, graphAt(10, 43)]),
    ),
    { kind: "saved", nodeRev: 43 },
  );
  assert.deepEqual(
    sent.splice(0).map((body) => body.base_revision),
    [7, 9],
  );
  assert.deepEqual(
    await model.savePlanNodeEdit(save, ports([graphAt(7, 41), graphAt(9, 44)], [refusal])),
    { kind: "conflict" },
  );
  await assert.rejects(
    model.savePlanNodeEdit(save, ports([graphAt(7, 41), graphAt(9, 41)], [refusal, refusal])),
    (failure) => failure === refusal,
  );
  await assert.rejects(
    model.savePlanNodeEdit(save, ports([graphAt(7, 41)], [new ApiError("bad", 422)])),
  );
});

test("a draft changes only the node fields that differ, and goes stale when the node moves", () => {
  assert.equal(model.planNodeChanges({}, node), null);
  assert.equal(
    model.planNodeChanges(
      { design: `${node.design}  `, doneWhen: node.completion_criteria.join("\n") },
      node,
    ),
    null,
  );
  assert.deepEqual(model.planNodeChanges({ design: "New", doneWhen: " One \n\nTwo" }, node), {
    design: "New",
    completion_criteria: ["One", "Two"],
  });
  assert.equal(model.planNodeDraftStale({ design: "New", nodeRev: 41 }, node), false);
  assert.equal(model.planNodeDraftStale({ design: "New", nodeRev: 40 }, node), true);
  assert.equal(model.planNodeDraftStale({ nodeRev: 40 }, node), false);
});

test("a revised card keeps the tap open; Park and Save hand back the hash to adopt", async () => {
  const revised = await model.tapPlanCard(
    "q1",
    { decision: "approve", pinned_sha256: "old" },
    async () => {
      throw new ApiError("revised", 409, { code: "plan_revised" });
    },
  );
  assert.deepEqual(revised, { kind: "revised" });
  const kept = await model.tapPlanCard(
    "q1",
    { decision: "park", pinned_sha256: "old" },
    async () => ({ outcome: "parked", pinned_sha256: "new" }),
  );
  assert.deepEqual(kept, { kind: "kept", revision: { outcome: "parked", pinned_sha256: "new" } });
  const parkRequest = model.planStartDecision(
    card(),
    model.initialPlanInputs(action(), null),
    node,
    "park",
  );
  assert.equal(parkRequest.decision, "park");
  assert.equal(parkRequest.start.node_rev, 41);
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
    start: {
      kind: "experiment_start",
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

test("episode kickoff cards are plan cards, and a branch card targets its branch", () => {
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

test("Auto-research cards use main and send edited launch inputs", () => {
  const auto = { kind: "auto_research", project_id: "p", invocation_ceiling: 4 };
  const own = card({ kind: "auto_research", action: auto });
  assert.equal(model.isPlanCardQuestion(question({ card: own })), true);
  assert.deepEqual(model.planGraphTarget(auto), { kind: "main" });
  const inputs = model.initialAutoResearchInputs(auto, null);
  assert.equal(inputs.invocationCeiling, 4);
  const edited = {
    ...inputs,
    instruction: "  Explore  ",
    branch: "study",
    codeWorktree: false,
    worktreeRepository: null,
    invocationCeiling: 7,
  };
  assert.equal(model.autoResearchInputsReady(edited), true);
  assert.equal(model.autoResearchInputsReady({ ...edited, instruction: "  " }), false);
  assert.equal(model.autoResearchInputsReady({ ...edited, invocationCeiling: 0 }), false);
  assert.equal(model.autoResearchInputsReady({ ...edited, invocationCeiling: 1.5 }), false);
  assert.equal(model.autoResearchInputsReady({ ...edited, branch: "main" }), false);
  assert.deepEqual(model.autoResearchStartDecision(own, edited).start, {
    kind: "auto_research",
    starting_instruction: "Explore",
    branch_name: "study",
    code_worktree: false,
    worktree_repository: null,
    invocation_ceiling: 7,
    provider: null,
    model: null,
    reasoning: null,
  });
});

test("Auto-research needs a ready orchestrator and a settled card is not open", () => {
  const own = card({
    kind: "auto_research",
    action: {
      kind: "auto_research",
      project_id: "p",
      invocation_ceiling: 4,
      starting_instruction: "Explore",
    },
  });
  const autoSnapshot = {
    ...snapshot,
    agent_profiles: { ...snapshot.agent_profiles, orchestrator: snapshot.agent_profiles.node_chat },
    repositories: [],
    project_truth_scope: [],
  };
  assert.doesNotMatch(
    startButton(render({ question: question({ card: own }), snapshot: autoSnapshot })),
    /disabled/,
  );
  assert.match(
    startButton(render({ question: question({ card: own }), snapshot: null })),
    /disabled/,
  );
  assert.equal(taskCards.taskCardView({ ...own, result: { outcome: "approved" } }).open, false);
});

test("both card kinds preserve explicit model and reasoning with the profile provider", () => {
  const fallback = { provider: "claude", model: "default", reasoning: "low", run_on: "m1" };
  const explicit = { provider: null, model: "chosen", reasoning: "high" };
  assert.deepEqual(model.initialPlanInputs(action(explicit), fallback).config, {
    ...fallback,
    model: "chosen",
    reasoning: "high",
  });
  assert.deepEqual(
    model.initialAutoResearchInputs(
      { kind: "auto_research", invocation_ceiling: 3, ...explicit },
      fallback,
    ).config,
    { ...fallback, model: "chosen", reasoning: "high" },
  );
});

test("a taken branch maps the Start refusal to the editable branch field", async () => {
  const outcome = await model.tapPlanCard(
    "q1",
    { decision: "approve", pinned_sha256: "sha" },
    async () => {
      throw new ApiError("", 409, { code: "branch_name_taken" });
    },
  );
  assert.equal(outcome.kind, "pending");
  assert.equal(outcome.branchRefusal, "branch_name_taken");
});
