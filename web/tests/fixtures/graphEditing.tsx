import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { DagView } from "../../src/views/GraphViews";
import {
  emptyHumanDraft,
  applyHumanDraft,
  stageNodeRemoval,
  stageCustomNode,
  stageEdgeAddition,
  stageEdgeRemoval,
  unstageEdgeRemoval,
  toHumanSyncRequest,
} from "../../src/humanDraft";
import type { GraphState } from "../../src/types";
import "../../src/styles.css";

const ontology = { types: [], fields: [], relations: [] };
const first = {
  id: "hyp/first",
  type: "hypothesis",
  title: "First claim",
  statement: "Claim",
  standing: "asserted",
  created_rev: 1,
  updated_rev: 1,
  source_refs: [],
  extension_fields: {},
} as const;
const second = {
  id: "ev/second",
  type: "evidence",
  title: "Second result",
  observation: "Result",
  origin: "analytic",
  standing: "asserted",
  created_rev: 1,
  updated_rev: 1,
  source_refs: [],
  extension_fields: {},
} as const;
const canonicalEdge = {
  id: "edge/canonical",
  source: first.id,
  target: second.id,
  relation: "contradicts",
  explanation: "Canonical comparison",
  layer: "epistemic",
} as const;
const graph = {
  revision: 1,
  nodes: { [first.id]: first, [second.id]: second },
  edges: { [canonicalEdge.id]: canonicalEdge },
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
  // A completed backend preview fixture; production resolves layers on the server.
  const previewGraph = applyHumanDraft(
    {
      ...currentGraph,
      edges: {
        ...currentGraph.edges,
        "edge/preview-only": {
          id: "edge/preview-only",
          source: first.id,
          target: second.id,
          relation: "relates_to",
          explanation: "Rule-created preview edge",
          layer: "meta",
        },
        ...Object.fromEntries(
          draft.added_edges.map((edge) => [edge.id, { ...edge, layer: "epistemic" }]),
        ),
      },
    },
    draft,
  );
  return (
    <>
      <button onClick={() => setDisabled(!disabled)}>Toggle read-only</button>
      <button onClick={() => setCurrentGraph({ ...graph, nodes: { [first.id]: first } })}>
        Remove source from fixture
      </button>
      <button onClick={() => setDraft((value) => stageNodeRemoval(value, currentGraph, first.id))}>
        Stage target removal
      </button>
      <DagView
        projectId="fixture"
        graph={previewGraph}
        trustView="working"
        viewportRef={viewport}
        onSelectNode={() => {}}
        mutationsDisabled={disabled}
        onStageCustomNode={(node) => setDraft((value) => stageCustomNode(value, node))}
        onStageEdge={(edge) => setDraft((value) => stageEdgeAddition(value, currentGraph, edge))}
        onRemoveEdge={(id) => setDraft((value) => stageEdgeRemoval(value, currentGraph, id))}
        onUndoRemoveEdge={(id) => setDraft((value) => unstageEdgeRemoval(value, currentGraph, id))}
        canonicalEdges={currentGraph.edges}
        draftRemovedNodeIds={draft.removed_node_ids}
        draftAddedNodeIds={Object.keys(draft.custom_nodes)}
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
