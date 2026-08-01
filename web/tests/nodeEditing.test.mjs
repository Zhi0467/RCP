import assert from "node:assert/strict";
import test from "node:test";

import { changedNodeFields, editableNodeFields, nodeEditDraft } from "../src/nodeEditing.ts";

const hypothesis = {
  id: "hyp/example",
  type: "hypothesis",
  title: "Existing title",
  standing: "accepted",
  created_rev: 2,
  updated_rev: 4,
  source_refs: [],
  extension_fields: {},
  statement: "Existing statement",
  rationale: "",
  predictions: ["First prediction", "Second prediction"],
  scope: "Single-domain evaluation",
  status: "active",
};

test("editable fields mirror the human-editable allowlist for every node type", () => {
  const keys = (type) => editableNodeFields({ ...hypothesis, type }).map((field) => field.key);
  assert.deepEqual(keys("research_question"), ["title", "question", "motivation", "scope"]);
  assert.deepEqual(keys("hypothesis"), ["title", "statement", "rationale", "predictions", "scope"]);
  assert.deepEqual(keys("decision"), ["title", "question", "options", "rationale", "consequences"]);
  assert.deepEqual(keys("experiment"), ["title", "objective", "design", "expected_outcomes", "interpretation_rules", "completion_criteria", "current_summary", "next_action"]);
  assert.deepEqual(keys("evidence"), ["title", "observation", "interpretation"]);
  assert.deepEqual(keys("blocker"), ["title", "description", "resolution_condition", "recommended_action"]);
});

test("active base and custom extension fields are editable as one complete object", () => {
  const ontology = {
    types: [{ name: "mechanism_hypothesis", definition: "Mechanism claim", base_type: "hypothesis", layer: "epistemic", deprecated: false }],
    fields: [
      { owner_type: "hypothesis", name: "prior", definition: "Prior", kind: "number", required: false, agent_writable: false, deprecated: false },
      { owner_type: "mechanism_hypothesis", name: "mechanism", definition: "Mechanism", kind: "text", required: true, agent_writable: true, deprecated: false },
      { owner_type: "mechanism_hypothesis", name: "old_note", definition: "Old note", kind: "text", required: false, agent_writable: true, deprecated: true },
    ],
    relations: [],
  };
  const node = {
    ...hypothesis,
    extension_type: "mechanism_hypothesis",
    extension_fields: { prior: 0.4, mechanism: "Old mechanism", old_note: "Still readable" },
  };
  const fields = editableNodeFields(node, ontology);
  assert.deepEqual(fields.slice(-2).map((field) => field.key), ["extension_fields.prior", "extension_fields.mechanism"]);
  assert.equal(fields.some((field) => field.key === "extension_fields.old_note"), false);
  const draft = nodeEditDraft(node, ontology);
  draft["extension_fields.mechanism"] = "Updated mechanism";
  assert.deepEqual(changedNodeFields(node, draft, ontology), {
    extension_fields: {
      prior: 0.4,
      mechanism: "Updated mechanism",
      old_note: "Still readable",
    },
  });
  draft["extension_fields.prior"] = "";
  assert.deepEqual(changedNodeFields(node, draft, ontology), {
    extension_fields: {
      mechanism: "Updated mechanism",
      old_note: "Still readable",
    },
  });
});

test("node drafts render lists one item per line and submit only normalized changes", () => {
  const draft = nodeEditDraft(hypothesis);
  assert.equal(draft.predictions, "First prediction\nSecond prediction");
  assert.equal(draft.scope, "Single-domain evaluation");
  assert.equal("confidence" in hypothesis, false);
  draft.title = "  Existing title  ";
  draft.predictions = "First prediction\n\n Revised second prediction  ";
  assert.deepEqual(changedNodeFields(hypothesis, draft), {
    predictions: ["First prediction", "Revised second prediction"],
  });
});

test("blank nullable prose becomes null while an existing null is unchanged", () => {
  const decision = {
    ...hypothesis,
    type: "decision",
    question: "Choose a method",
    options: ["A", "B"],
    rationale: "Current reason",
    consequences: [],
  };
  const draft = nodeEditDraft(decision);
  draft.rationale = "   ";
  assert.deepEqual(changedNodeFields(decision, draft), { rationale: null });
  const alreadyBlank = { ...decision, rationale: null };
  assert.deepEqual(changedNodeFields(alreadyBlank, nodeEditDraft(alreadyBlank)), {});
});
