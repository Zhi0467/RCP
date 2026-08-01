import assert from "node:assert/strict";
import test from "node:test";

import {
  applyHumanDraft,
  deserializeHumanDraft,
  emptyHumanDraft,
  humanDraftChangeCount,
  humanDraftStorageKey,
  normalizeHumanDraft,
  serializeHumanDraft,
  stageAmbiguityDecision,
  stageNodeEdit,
  stageNodeEditStart,
  stageNodeStanding,
  stageProposalDecision,
  stageCustomNode,
  stageOntology,
  unstageCustomNode,
  toHumanSyncRequest,
} from "../src/humanDraft.ts";

const graph = {
  revision: 4,
  nodes: {
    "hyp/example": {
      id: "hyp/example",
      type: "hypothesis",
      title: "Existing title",
      standing: "accepted",
      created_rev: 2,
      updated_rev: 4,
      source_refs: [],
      extension_fields: {},
      statement: "Existing statement",
    },
  },
  edges: {},
  proposals: {},
  ambiguities: {},
  glossary: {},
  ontology: { types: [], fields: [], relations: [] },
  validation_messages: [],
  belief_transitions: [],
  replay_status: "complete",
  replay_failure: null,
};

test("normalization drops node fields and standing that match canonical state", () => {
  const draft = {
    ...emptyHumanDraft(4),
    nodes: {
      "hyp/example": {
        base_updated_rev: 4,
        changes: { title: "Existing title" },
        standing: "accepted",
        standing_origin: "judgment",
      },
    },
  };
  const normalized = normalizeHumanDraft(draft, graph);
  assert.deepEqual(normalized.nodes, {});
  assert.equal(humanDraftChangeCount(normalized), 0);
});

test("wording edits clear an existing judgment and disappear when fully reverted", () => {
  const editing = stageNodeEditStart(emptyHumanDraft(4), graph, "hyp/example");
  assert.equal(editing.nodes["hyp/example"].standing, "asserted");
  const edited = stageNodeEdit(editing, graph, "hyp/example", { title: "Revised" });
  assert.equal(edited.nodes["hyp/example"].standing, "asserted");
  assert.equal(applyHumanDraft(graph, edited).nodes["hyp/example"].draft_touched, true);
  assert.equal(applyHumanDraft(graph, edited).nodes["hyp/example"].title, "Revised");

  const reverted = stageNodeEdit(edited, graph, "hyp/example", { title: "Existing title" });
  assert.equal(humanDraftChangeCount(reverted), 0);
});

test("judgments, proposal decisions, and ambiguity decisions are reversible", () => {
  let draft = stageNodeStanding(emptyHumanDraft(4), graph, "hyp/example", "contested");
  draft = stageProposalDecision(draft, "proposal/1", "approved");
  draft = stageAmbiguityDecision(draft, "ambiguity/1", "resolved");
  assert.equal(humanDraftChangeCount(draft), 3);

  draft = stageNodeStanding(draft, graph, "hyp/example", "accepted");
  draft = stageProposalDecision(draft, "proposal/1", null);
  draft = stageAmbiguityDecision(draft, "ambiguity/1", null);
  assert.equal(humanDraftChangeCount(draft), 0);
});

test("serialization survives localStorage round trips and request conversion strips editor metadata", () => {
  let draft = stageNodeEdit(emptyHumanDraft(4), graph, "hyp/example", {
    title: "Revised",
    statement: "Sharper statement",
  });
  draft = stageProposalDecision(draft, "proposal/1", "rejected");
  draft = stageAmbiguityDecision(draft, "ambiguity/1", "dismissed");
  const restored = deserializeHumanDraft(serializeHumanDraft(draft));
  assert.deepEqual(restored, draft);
  assert.equal(humanDraftStorageKey("project one"), "rcp:human-draft:project one");
  assert.equal(deserializeHumanDraft("not json"), null);

  assert.deepEqual(toHumanSyncRequest(restored), {
    base_revision: 4,
    nodes: [
      {
        node_id: "hyp/example",
        base_updated_rev: 4,
        changes: { title: "Revised", statement: "Sharper statement" },
        standing: "asserted",
      },
    ],
    proposals: [{ proposal_id: "proposal/1", decision: "rejected" }],
    ambiguities: [{ ambiguity_id: "ambiguity/1", status: "dismissed" }],
    ontology: null,
    custom_nodes: [],
  });
});

test("ontology and custom nodes round trip, count, present, and serialize through project Sync", () => {
  const ontology = {
    types: [
      {
        name: "mechanism_hypothesis",
        definition: "A causal mechanism claim.",
        base_type: "hypothesis",
        layer: "epistemic",
        deprecated: false,
      },
    ],
    fields: [
      {
        owner_type: "mechanism_hypothesis",
        name: "mechanism",
        definition: "The causal mechanism.",
        kind: "text",
        required: true,
        agent_writable: false,
        deprecated: false,
      },
    ],
    relations: [],
  };
  const customNode = {
    id: "mechanism_hypothesis/replanning",
    type: "hypothesis",
    extension_type: "mechanism_hypothesis",
    extension_fields: { mechanism: "Replanning restores update directions." },
    title: "Replanning mechanism",
    statement: "Replanning preserves plasticity.",
    standing: "asserted",
    created_rev: 0,
    updated_rev: 0,
    source_refs: [],
  };
  let draft = stageOntology(emptyHumanDraft(4), graph, ontology);
  draft = stageCustomNode(draft, customNode);
  assert.equal(humanDraftChangeCount(draft), 2);
  assert.deepEqual(deserializeHumanDraft(serializeHumanDraft(draft)), draft);
  const presented = applyHumanDraft(graph, draft);
  assert.deepEqual(presented.ontology, ontology);
  assert.equal(presented.nodes[customNode.id].draft_touched, true);
  assert.deepEqual(toHumanSyncRequest(draft), {
    base_revision: 4,
    nodes: [],
    proposals: [],
    ambiguities: [],
    ontology,
    custom_nodes: [customNode],
  });
  const ontologyOnly = unstageCustomNode(draft, customNode.id);
  assert.equal(humanDraftChangeCount(ontologyOnly), 1);
  assert.deepEqual(toHumanSyncRequest(ontologyOnly).custom_nodes, []);
});
