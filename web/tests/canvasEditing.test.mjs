import assert from "node:assert/strict";
import test from "node:test";
import {
  relationChoices,
  newNodeTypeChoices,
  buildCanvasNode,
  requiredCanvasFields,
} from "../src/graph/canvasEditing.ts";
import { FLOW_ORDER, primaryField, carriesOrigin } from "../src/graph/researchType.ts";

const empty = { types: [], fields: [], relations: [] };
const relation = (name, source_types, target_types, extra = {}) => ({
  name,
  source_types,
  target_types,
  same_type: false,
  accepts_expectation: false,
  assessment_required_for: [],
  ...extra,
});
const options = {
  node_prefixes: Object.fromEntries(FLOW_ORDER.map((type, i) => [type, `n${i}`])),
  relations: [
    relation("tests", ["experiment"], ["hypothesis"]),
    relation("supports", ["evidence"], ["hypothesis"], {
      accepts_expectation: true,
      assessment_required_for: [{ source_type: "evidence", target_type: "hypothesis" }],
    }),
    relation("duplicate_of", FLOW_ORDER, FLOW_ORDER, { same_type: true }),
  ],
};
const node = (id, type, extension_type = null) => ({ id, type, extension_type });
const nodes = { a: node("a", "hypothesis"), b: node("b", "evidence") };

test("relations use base types and retain oriented assessment and expectation", () => {
  assert.deepEqual(relationChoices(options, empty, nodes, [], "a", "b"), [
    {
      relation: "supports",
      source: "b",
      target: "a",
      meta: false,
      needsAssessment: true,
      acceptsExpectation: true,
    },
  ]);
  assert.deepEqual(relationChoices(options, empty, nodes, [], "a", "a"), []);
  assert.deepEqual(relationChoices(options, empty, nodes, [], "a", "missing"), []);
});

test("existing relations are excluded only for the same orientation", () => {
  const pair = { a: nodes.a, b: node("b", "hypothesis") };
  assert.deepEqual(
    relationChoices(
      options,
      empty,
      pair,
      [{ source: "a", target: "b", relation: "duplicate_of" }],
      "a",
      "b",
    ).map((c) => c.source),
    ["b"],
  );
});

test("meta relations compare base types only and sort last", () => {
  const pair = { a: node("a", "hypothesis", "claim"), b: node("b", "hypothesis", "claim") };
  const ontology = {
    ...empty,
    relations: [
      relation("connects", ["claim"], ["claim"]),
      relation("base_only", ["hypothesis"], ["hypothesis"]),
      relation("retired", ["claim"], ["claim"], { deprecated: true }),
    ],
  };
  assert.deepEqual(
    relationChoices(options, ontology, pair, [], "a", "b").map((c) => [c.relation, c.source]),
    [
      ["connects", "a"],
      ["connects", "b"],
      ["duplicate_of", "a"],
      ["duplicate_of", "b"],
    ],
  );
  pair.b.extension_type = "other";
  assert.equal(
    relationChoices(options, ontology, pair, [], "a", "b").some((c) => c.meta),
    true,
  );
});

test("new type choices preserve flow order and append only active custom types", () => {
  const ontology = {
    ...empty,
    types: [
      { name: "claim", base_type: "hypothesis" },
      { name: "retired", base_type: "hypothesis", deprecated: true },
    ],
  };
  assert.deepEqual(
    newNodeTypeChoices(options, ontology).map((c) => c.name),
    [...FLOW_ORDER, "claim"],
  );
});

test("linked type choices filter unconnectable types and choose preferred direction", () => {
  assert.deepEqual(
    newNodeTypeChoices(options, empty, nodes.a).map((c) => [c.name, c.relation, c.outgoing]),
    [
      ["hypothesis", "duplicate_of", true],
      ["experiment", "tests", false],
      ["evidence", "supports", false],
    ],
  );
});

test("built-in nodes use prefixes, primary fields, and explicit origin where required", () => {
  for (const choice of newNodeTypeChoices(options, empty)) {
    const result = buildCanvasNode(choice, options, "  A Title!  ", new Set());
    assert.equal(result.id, `${options.node_prefixes[choice.name]}/a-title`);
    assert.equal(result.title, "A Title!");
    assert.equal(result[primaryField(choice.base_type)], result.title);
    assert.equal(result.origin, carriesOrigin(choice.base_type) ? "unknown" : undefined);
    assert.equal(result.extension_type, null);
  }
});

test("custom nodes preserve fields and generate bounded unique nonempty slugs", () => {
  const choice = { name: "claim", base_type: "hypothesis" };
  const result = buildCanvasNode(
    choice,
    options,
    "A title",
    new Set(["claim/a-title", "claim/a-title-2"]),
    { score: 0 },
  );
  assert.equal(result.id, "claim/a-title-3");
  assert.equal(result.extension_type, "claim");
  assert.deepEqual(result.extension_fields, { score: 0 });
  assert.ok(buildCanvasNode(choice, options, "long title ".repeat(50), new Set()).id.length < 100);
  assert.equal(buildCanvasNode(choice, options, "研究", new Set()).id, "claim/node");
});

test("required fields include active base and custom owners only", () => {
  const ontology = {
    ...empty,
    fields: [
      { name: "base", owner_type: "hypothesis", required: true },
      { name: "custom", owner_type: "claim", required: true },
      { name: "optional", owner_type: "claim", required: false },
      { name: "old", owner_type: "claim", required: true, deprecated: true },
      { name: "other", owner_type: "other", required: true },
    ],
  };
  assert.deepEqual(
    requiredCanvasFields(ontology, { name: "claim", base_type: "hypothesis" }).map((f) => f.name),
    ["base", "custom"],
  );
});
