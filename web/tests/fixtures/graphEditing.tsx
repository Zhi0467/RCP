import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { DagView } from "../../src/views/GraphViews";
import { makeHumanNode } from "../../src/ontologyEditing";
import {
  emptyHumanDraft,
  stageCustomNode,
  stageEdgeAddition,
  stageEdgeRemoval,
  unstageEdgeRemoval,
  toHumanSyncRequest,
} from "../../src/humanDraft";
import type { GraphState } from "../../src/types";
import "../../src/styles.css";

const ontology = { types: [], fields: [], relations: [] };
const first = makeHumanNode(ontology, "hypothesis", "first", "First claim", "Claim", undefined, {});
const second = makeHumanNode(
  ontology,
  "evidence",
  "second",
  "Second result",
  "Result",
  "analytic",
  {},
);
const graph = {
  revision: 1,
  nodes: { [first.id]: first, [second.id]: second },
  edges: {},
  ontology,
  proposals: {},
  glossary: {},
  ambiguities: {},
  validation_messages: [],
  belief_transitions: [],
  replay_status: "complete",
  replay_failure: null,
} as GraphState;

function Fixture() {
  const [currentGraph, setCurrentGraph] = useState(graph);
  const [draft, setDraft] = useState(emptyHumanDraft(1));
  const [disabled, setDisabled] = useState(false);
  const viewport = useRef(null);
  return (
    <>
      <button onClick={() => setDisabled(!disabled)}>Toggle read-only</button>
      <button onClick={() => setCurrentGraph({ ...graph, nodes: { [first.id]: first } })}>
        Remove source from fixture
      </button>
      <DagView
        projectId="fixture"
        graph={currentGraph}
        trustView="working"
        viewportRef={viewport}
        onSelectNode={() => {}}
        mutationsDisabled={disabled}
        onStageCustomNode={(node) => setDraft((value) => stageCustomNode(value, node))}
        onStageEdge={(edge) => setDraft((value) => stageEdgeAddition(value, edge))}
        onRemoveEdge={(id) => setDraft((value) => stageEdgeRemoval(value, id))}
        onUndoRemoveEdge={(id) => setDraft((value) => unstageEdgeRemoval(value, id))}
        draftAddedEdges={draft.added_edges}
        draftRemovedEdgeIds={draft.removed_edge_ids}
      />
      <output aria-label="Staged request">
        {JSON.stringify(toHumanSyncRequest(draft, graph))}
      </output>
    </>
  );
}
createRoot(document.getElementById("root")!).render(<Fixture />);
