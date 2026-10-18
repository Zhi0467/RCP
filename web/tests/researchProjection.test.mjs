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
  node("e/survey", "evidence", { origin: "external_publication" }),
  node("b/gpu", "blocker", { status: "open" }),
  node("e/budget", "evidence"),
  node("d/seed-count", "decision", { status: "open" }),
  node("e/variance", "evidence"),
  node("b/seed-budget", "blocker", { status: "open" }),
  node("d/quota", "decision", { status: "open" }),
  node("e/quota-cost", "evidence"),
  node("d/gpu-vendor", "decision", { status: "open" }),
  node("b/cluster", "blocker", { status: "open" }),
  node("d/region", "decision", { status: "open" }),
  node("x/region-bench", "experiment", { status: "completed" }),
  node("e/region", "evidence"),
  node("x/batch-sweep", "experiment", { status: "completed" }),
  node("e/sweep", "evidence"),
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
  edge("e/budget", "informs", "d/batch"),
  edge("x/done", "produces", "e/for"),
  edge("x/done", "produces", "e/side"),
  edge("e/for", "supports", "h/shared"),
  edge("e/paper", "refutes", "h/shared"),
  edge("q/sub", "has_decision", "d/metric"),
  edge("x/probe", "produces", "e/probe"),
  edge("e/probe", "informs", "d/metric"),
  edge("e/survey", "informs", "d/metric"),
  edge("x/probe", "blocked_by", "b/gpu"),
  edge("x/probe", "governed_by", "d/seed-count"),
  edge("e/variance", "informs", "d/seed-count"),
  edge("d/seed-count", "blocked_by", "b/seed-budget"),
  edge("b/queue", "requires_decision", "d/quota"),
  edge("e/quota-cost", "informs", "d/quota"),
  edge("b/gpu", "requires_decision", "d/gpu-vendor"),
  edge("q/sub", "blocked_by", "b/cluster"),
  edge("b/cluster", "requires_decision", "d/region"),
  edge("x/region-bench", "governed_by", "d/region"),
  edge("x/region-bench", "produces", "e/region"),
  edge("x/batch-sweep", "governed_by", "d/batch"),
  edge("x/batch-sweep", "produces", "e/sweep"),
];

test("a Hypothesis page gathers its Experiments' inputs and weighs each result", () => {
  const page = buildResearchDrilldown(nodes, edges).hypotheses.get("h/shared");

  assert.deepEqual(ids(page.questions).sort(), ["q/other", "q/sub"]);
  assert.deepEqual(ids(page.inputs.nodes).sort(), ["b/approval", "b/queue", "d/batch", "d/quota"]);
  assert.deepEqual(ids(page.inputs.evidence).sort(), ["e/budget", "e/quota-cost"]);
  assert.equal(page.inputs.openDecisions, 2);
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
  assert.deepEqual(ids(sub.loose), [
    "d/metric",
    "b/cluster",
    "d/region",
    "x/batch-sweep",
    "e/sweep",
    "d/batch",
    "x/probe",
    "e/probe",
    "d/seed-count",
    "b/gpu",
    "x/region-bench",
    "e/region",
    "b/approval",
    "b/seed-budget",
    "d/gpu-vendor",
    "e/survey",
    "e/budget",
    "e/variance",
  ]);

  // Card counts stay one relation away: the root does not count its subquestion's work.
  const root = drilldown.roots.find((item) => item.node.id === "q/root");
  assert.deepEqual(
    [root.subquestionCount, root.hypothesisCount, root.openDecisions, root.openBlockers],
    [1, 0, 0, 0],
  );
  const subCard = drilldown.questions.get("q/root").subquestions[0];
  assert.deepEqual([subCard.subquestionCount, subCard.hypothesisCount], [0, 1]);
  const shared = sub.hypotheses[0];
  assert.deepEqual(
    [shared.openDecisions, shared.openBlockers, shared.activeExperiments],
    [2, 2, 1],
  );
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
