import assert from "node:assert/strict";
import test from "node:test";

import { buildResearchDrilldown } from "../src/graph/researchProjection.ts";

const node = (id, type, fields = {}) => ({
  id,
  type,
  title: id,
  standing: "asserted",
  source_refs: [],
  ...fields,
});
let edgeCount = 0;
const edge = (source, relation, target) => ({
  id: `edge-${(edgeCount += 1)}`,
  source,
  target,
  relation,
});
const ids = (nodes) => nodes.map((item) => item.id);

const nodes = [
  node("q/root", "research_question"),
  node("q/sub", "research_question"),
  node("q/other", "research_question"),
  node("h/shared", "hypothesis"),
  node("x/run", "experiment", { status: "running" }),
  node("x/done", "experiment", { status: "completed" }),
  node("d/batch", "decision", { status: "open" }),
  node("b/queue", "blocker", { status: "open" }),
  node("b/approval", "blocker", { status: "open" }),
  node("e/for", "evidence"),
  node("e/side", "evidence"),
  node("e/paper", "evidence"),
  node("d/metric", "decision", { status: "decided" }),
  node("x/probe", "experiment", { status: "completed" }),
  node("e/probe", "evidence"),
  node("b/gpu", "blocker", { status: "open" }),
  node("orphan", "decision"),
];
const edges = [
  edge("q/root", "has_subquestion", "q/sub"),
  edge("q/sub", "has_hypothesis", "h/shared"),
  edge("q/other", "has_hypothesis", "h/shared"),
  edge("x/run", "tests", "h/shared"),
  edge("x/done", "tests", "h/shared"),
  edge("x/run", "governed_by", "d/batch"),
  edge("x/run", "blocked_by", "b/queue"),
  edge("d/batch", "blocked_by", "b/approval"),
  edge("x/done", "produces", "e/for"),
  edge("x/done", "produces", "e/side"),
  edge("e/for", "supports", "h/shared"),
  edge("e/paper", "refutes", "h/shared"),
  edge("q/sub", "has_decision", "d/metric"),
  edge("x/probe", "produces", "e/probe"),
  edge("e/probe", "informs", "d/metric"),
  edge("x/probe", "blocked_by", "b/gpu"),
];

test("a Hypothesis page gathers its Experiments' inputs and weighs each result", () => {
  const page = buildResearchDrilldown(nodes, edges).hypotheses.get("h/shared");

  assert.deepEqual(ids(page.questions).sort(), ["q/other", "q/sub"]);
  assert.deepEqual(ids(page.inputs.nodes).sort(), ["b/approval", "b/queue", "d/batch"]);
  assert.equal(page.inputs.openDecisions, 1);
  assert.equal(page.inputs.openBlockers, 2);

  const lanes = Object.fromEntries(page.experiments.map((lane) => [lane.node.id, lane]));
  assert.equal(lanes["x/run"].active, true);
  assert.equal(lanes["x/run"].blocked, true);
  assert.deepEqual(
    lanes["x/done"].evidence.map((item) => [item.node.id, item.verdict]),
    [
      ["e/for", "for"],
      ["e/side", null],
    ],
  );
  // Evidence from no listed Experiment still counts toward the Hypothesis.
  assert.deepEqual(page.tally, { for: 1, against: 1, unclear: 0 });
});

test("question pages share Hypotheses and keep work that tests none", () => {
  const drilldown = buildResearchDrilldown(nodes, edges);

  assert.deepEqual(ids(drilldown.roots.map((item) => item.node)).sort(), ["q/other", "q/root"]);
  const sub = drilldown.questions.get("q/sub");
  assert.deepEqual(ids(sub.hypotheses.map((item) => item.node)), ["h/shared"]);
  assert.deepEqual(ids(drilldown.questions.get("q/other").hypotheses.map((item) => item.node)), [
    "h/shared",
  ]);
  assert.deepEqual(ids(sub.loose), ["d/metric", "x/probe", "e/probe", "b/gpu"]);

  const root = drilldown.roots.find((item) => item.node.id === "q/root").rollup;
  assert.equal(root.activeExperiments, 1);
  assert.equal(root.openBlockers, 3);
  assert.ok(root.memberIds.includes("e/probe"));
  assert.deepEqual(ids(drilldown.unplaced), ["orphan"]);
});

test("a question cycle with no root still gets a card", () => {
  const drilldown = buildResearchDrilldown(
    [node("q/a", "research_question"), node("q/b", "research_question")],
    [edge("q/a", "has_subquestion", "q/b"), edge("q/b", "has_subquestion", "q/a")],
  );

  assert.deepEqual(ids(drilldown.roots.map((item) => item.node)), ["q/a"]);
  assert.deepEqual(drilldown.unplaced, []);
});
