import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../src/api.ts";
import {
  applyHumanDraft,
  deserializeHumanDraft,
  emptyHumanDraft,
  humanDraftChangeCount,
  humanDraftStorageKey,
  humanSyncFailure,
  normalizeHumanDraft,
  proposalTargetsNode,
  serializeHumanDraft,
  stageAmbiguityDecision,
  stageDecisionChoice,
  stageNodeEdit,
  stageNodeEditStart,
  stageNodeRemoval,
  stageNodeStanding,
  stageProposalDecision,
  stageCustomNode,
  stageOntology,
  unstageCustomNode,
  unstageNodeRemoval,
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

test("Blocker lifecycle edits invalidate prior judgment and can reopen attention", () => {
  const acceptedOpen = {
    id: "blocker/accepted-open",
    type: "blocker",
    title: "Accepted open blocker",
    standing: "accepted",
    status: "open",
    blocker_type: "scientific",
    description: "A result is missing.",
    resolution_condition: "Record the result.",
    recommended_action: null,
    created_rev: 2,
    updated_rev: 4,
    source_refs: [],
    extension_fields: {},
  };
  const resolvedAsserted = {
    ...acceptedOpen,
    id: "blocker/resolved-asserted",
    title: "Resolved asserted blocker",
    standing: "asserted",
    status: "resolved",
  };
  const lifecycleGraph = {
    ...graph,
    nodes: {
      ...graph.nodes,
      [acceptedOpen.id]: acceptedOpen,
      [resolvedAsserted.id]: resolvedAsserted,
    },
  };

  const resolvingStart = stageNodeEditStart(emptyHumanDraft(4), lifecycleGraph, acceptedOpen.id);
  const resolving = stageNodeEdit(resolvingStart, lifecycleGraph, acceptedOpen.id, {
    status: "resolved",
  });
  assert.deepEqual(resolving.nodes[acceptedOpen.id].changes, { status: "resolved" });
  assert.equal(resolving.nodes[acceptedOpen.id].standing, "asserted");
  assert.equal(
    applyHumanDraft(lifecycleGraph, resolving).nodes[acceptedOpen.id].status,
    "resolved",
  );

  const reopeningStart = stageNodeEditStart(
    emptyHumanDraft(4),
    lifecycleGraph,
    resolvedAsserted.id,
  );
  assert.equal(reopeningStart.nodes[resolvedAsserted.id].standing, "asserted");
  const reopening = stageNodeEdit(reopeningStart, lifecycleGraph, resolvedAsserted.id, {
    status: "open",
  });
  assert.deepEqual(reopening.nodes[resolvedAsserted.id].changes, { status: "open" });
  const reopened = applyHumanDraft(lifecycleGraph, reopening).nodes[resolvedAsserted.id];
  assert.equal(reopened.status, "open");
  assert.equal(reopened.standing, "asserted");
});

test("judgments, proposal decisions, and ambiguity decisions are reversible", () => {
  let draft = stageNodeStanding(emptyHumanDraft(4), graph, "hyp/example", "contested");
  draft = stageProposalDecision(draft, graph, "proposal/1", "approved");
  draft = stageAmbiguityDecision(draft, "ambiguity/1", "resolved");
  assert.equal(humanDraftChangeCount(draft), 3);

  draft = stageNodeStanding(draft, graph, "hyp/example", "accepted");
  draft = stageProposalDecision(draft, graph, "proposal/1", null);
  draft = stageAmbiguityDecision(draft, "ambiguity/1", null);
  assert.equal(humanDraftChangeCount(draft), 0);
});

test("direct Decision choices merge with wording edits and supersede targeted proposal resolutions", () => {
  const decision = {
    id: "dec/resource",
    type: "decision",
    title: "Choose resource level",
    question: "Which resource level should the experiment use?",
    options: ["Small", "Medium", "Large"],
    selected_option: null,
    status: "open",
    standing: "asserted",
    created_rev: 2,
    updated_rev: 4,
    source_refs: [],
    extension_fields: {},
  };
  const targeted = {
    id: "proposal/targeted",
    title: "Use Medium",
    card: { situation_cold: "", why_human_now: "", consequences: "", decision_needed: "" },
    ops: [
      {
        op: "update_nodes",
        nodes: [{ id: decision.id, changes: { selected_option: "Medium" } }],
      },
    ],
    related_node_ids: [decision.id],
    base_rev: 4,
    status: "pending",
  };
  const relatedOnly = {
    ...targeted,
    id: "proposal/related-only",
    title: "Revise the hypothesis",
    ops: [
      {
        op: "update_nodes",
        nodes: [{ id: "hyp/example", changes: { statement: "Revised by proposal" } }],
      },
    ],
  };
  const decisionGraph = {
    ...graph,
    nodes: { ...graph.nodes, [decision.id]: decision },
    proposals: { [targeted.id]: targeted, [relatedOnly.id]: relatedOnly },
  };

  let draft = stageNodeEdit(emptyHumanDraft(4), decisionGraph, decision.id, {
    title: "Choose the initial resource level",
  });
  draft = stageProposalDecision(draft, decisionGraph, targeted.id, "approved");
  draft = stageProposalDecision(draft, decisionGraph, relatedOnly.id, "rejected");
  draft = stageDecisionChoice(draft, decisionGraph, decision.id, "Medium");

  assert.deepEqual(draft.nodes[decision.id], {
    base_updated_rev: 4,
    changes: {
      title: "Choose the initial resource level",
      selected_option: "Medium",
      status: "decided",
    },
    standing: "accepted",
    standing_origin: "judgment",
  });
  assert.deepEqual(draft.proposals, {
    [relatedOnly.id]: { decision: "rejected" },
  });
  assert.equal(proposalTargetsNode(targeted, decision.id), true);
  assert.equal(proposalTargetsNode(relatedOnly, decision.id), false);
  assert.equal(humanDraftChangeCount(draft), 5);

  const presented = applyHumanDraft(decisionGraph, draft).nodes[decision.id];
  assert.equal(presented.selected_option, "Medium");
  assert.equal(presented.status, "decided");
  assert.equal(presented.standing, "accepted");
  assert.equal(presented.draft_touched, true);

  const restored = deserializeHumanDraft(serializeHumanDraft(draft));
  assert.deepEqual(restored, draft);
  assert.deepEqual(toHumanSyncRequest(restored), {
    base_revision: 4,
    removed_node_ids: [],
    nodes: [
      {
        node_id: decision.id,
        base_updated_rev: 4,
        changes: {
          title: "Choose the initial resource level",
          selected_option: "Medium",
          status: "decided",
        },
        standing: "accepted",
      },
    ],
    proposals: [{ proposal_id: relatedOnly.id, decision: "rejected" }],
    ambiguities: [],
    ontology: null,
    custom_nodes: [],
  });

  const replaced = stageDecisionChoice(restored, decisionGraph, decision.id, "Large");
  assert.equal(replaced.nodes[decision.id].changes.selected_option, "Large");
  assert.equal(stageDecisionChoice(replaced, decisionGraph, decision.id, "Unlisted"), replaced);

  const editedAfterChoice = stageNodeEdit(replaced, decisionGraph, decision.id, {
    rationale: "Use the larger run after the pilot.",
  });
  assert.equal(editedAfterChoice.nodes[decision.id].changes.selected_option, "Large");
  assert.equal(editedAfterChoice.nodes[decision.id].changes.status, "decided");
  assert.equal(editedAfterChoice.nodes[decision.id].standing_origin, undefined);

  let reverseOrder = stageDecisionChoice(emptyHumanDraft(4), decisionGraph, decision.id, "Small");
  reverseOrder = stageProposalDecision(reverseOrder, decisionGraph, targeted.id, "approved");
  assert.deepEqual(reverseOrder.proposals, {});

  const restoredWithTargetedResolution = deserializeHumanDraft(
    serializeHumanDraft({
      ...reverseOrder,
      proposals: { [targeted.id]: { decision: "rejected" } },
    }),
  );
  assert.deepEqual(
    normalizeHumanDraft(restoredWithTargetedResolution, decisionGraph).proposals,
    {},
  );
});

test("serialization survives localStorage round trips and request conversion strips editor metadata", () => {
  let draft = stageNodeEdit(emptyHumanDraft(4), graph, "hyp/example", {
    title: "Revised",
    statement: "Sharper statement",
  });
  draft = stageProposalDecision(draft, graph, "proposal/1", "rejected");
  draft = stageAmbiguityDecision(draft, "ambiguity/1", "dismissed");
  const restored = deserializeHumanDraft(serializeHumanDraft(draft));
  assert.deepEqual(restored, draft);
  assert.equal(humanDraftStorageKey("project one"), "rcp:human-draft:project one");
  assert.equal(deserializeHumanDraft("not json"), null);

  assert.deepEqual(toHumanSyncRequest(restored), {
    base_revision: 4,
    removed_node_ids: [],
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
    removed_node_ids: [],
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

test("node removal is persistent, reversible, normalized, and mutually exclusive with node changes", () => {
  const contested = {
    ...graph.nodes["hyp/example"],
    id: "hyp/removable",
    title: "Removable hypothesis",
    standing: "contested",
  };
  const experiment = {
    ...contested,
    id: "exp/running",
    type: "experiment",
    title: "Running experiment",
    standing: "asserted",
    attempts: [],
  };
  const removalGraph = {
    ...graph,
    nodes: { ...graph.nodes, [contested.id]: contested, [experiment.id]: experiment },
    edges: {
      "edge/incident": {
        id: "edge/incident",
        source: contested.id,
        target: "hyp/example",
      },
    },
  };

  let draft = stageNodeRemoval(emptyHumanDraft(4), removalGraph, contested.id);
  assert.deepEqual(draft.removed_node_ids, [contested.id]);
  assert.equal(humanDraftChangeCount(draft), 1);
  assert.equal(applyHumanDraft(removalGraph, draft).nodes[contested.id].draft_touched, true);
  assert.deepEqual(toHumanSyncRequest(draft).removed_node_ids, [contested.id]);

  assert.equal(stageNodeStanding(draft, removalGraph, contested.id, "accepted"), draft);
  assert.equal(stageNodeEdit(draft, removalGraph, contested.id, { title: "Ignored" }), draft);

  draft = unstageNodeRemoval(draft, contested.id);
  assert.equal(humanDraftChangeCount(draft), 0);

  const changed = stageNodeStanding(emptyHumanDraft(4), removalGraph, contested.id, "asserted");
  assert.equal(stageNodeRemoval(changed, removalGraph, contested.id), changed);
  assert.deepEqual(
    stageNodeRemoval(emptyHumanDraft(4), removalGraph, "hyp/example").removed_node_ids,
    [],
  );
  assert.deepEqual(
    stageNodeRemoval(emptyHumanDraft(4), removalGraph, experiment.id, true).removed_node_ids,
    [],
  );

  const vanished = { ...draft, removed_node_ids: ["hyp/missing"] };
  assert.deepEqual(normalizeHumanDraft(vanished, removalGraph).removed_node_ids, []);

  const legacy = JSON.parse(serializeHumanDraft(emptyHumanDraft(4)));
  delete legacy.removed_node_ids;
  assert.deepEqual(deserializeHumanDraft(JSON.stringify(legacy)).removed_node_ids, []);
});

test("Sync conflicts preserve exact removal guards and rewrite only revision conflicts", () => {
  const accepted =
    "Accepted node hyp/example cannot be removed; withdraw its acceptance and Sync before removing it.";
  assert.deepEqual(humanSyncFailure(new ApiError(accepted, 409)), {
    text: accepted,
    revisionConflict: false,
  });

  const active =
    "Experiment exp/running cannot be removed while its bounded experiment loop is active.";
  assert.deepEqual(humanSyncFailure(new ApiError(active, 409)), {
    text: active,
    revisionConflict: false,
  });

  assert.deepEqual(
    humanSyncFailure(new ApiError("The graph changed after this draft began.", 409)),
    {
      text: "Draft base is stale. Reset the draft before syncing.",
      revisionConflict: true,
    },
  );
});
