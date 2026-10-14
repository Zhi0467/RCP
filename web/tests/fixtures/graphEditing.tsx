import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { DagView } from "../../src/graph/GraphViews";
import { emptyHumanDraft, type HumanDraft } from "../../src/graph/humanDraft";
import type { GraphEditingProps } from "../../src/graph/canvasEditing";
import type {
  GraphEditOptions,
  GraphNode,
  GraphState,
  NewEdge,
  NewNode,
} from "../../src/core/types";
import "../../src/styles.css";

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
} as GraphNode;
const second = {
  ...first,
  id: "ev/second",
  type: "evidence",
  title: "Second result",
  observation: "Result",
  origin: "analytic",
} as GraphNode;
const duplicate = { ...first, id: "hyp/duplicate", title: "Duplicate claim" };
const accepted = {
  ...first,
  id: "hyp/accepted",
  title: "Accepted claim",
  standing: "accepted",
} as GraphNode;
const graph = {
  revision: 1,
  nodes: Object.fromEntries([first, second, duplicate, accepted].map((node) => [node.id, node])),
  edges: {
    "edge/canonical": {
      id: "edge/canonical",
      source: first.id,
      target: second.id,
      relation: "contradicts",
      explanation: "Canonical comparison",
      layer: "epistemic",
    },
  },
  ontology: { types: [], fields: [], relations: [] },
  proposals: {},
  glossary: {},
  ambiguities: {},
  validation_messages: [],
  belief_transitions: [],
  replay_status: "complete",
  replay_failure: null,
} as GraphState;
const editOptions: GraphEditOptions = {
  node_prefixes: {
    research_question: "rq",
    hypothesis: "hyp",
    experiment: "exp",
    evidence: "ev",
    decision: "dec",
    blocker: "blk",
  },
  relations: [
    {
      name: "supports",
      source_types: ["evidence"],
      target_types: ["hypothesis"],
      same_type: false,
      assessment_required_for: [{ source_type: "evidence", target_type: "hypothesis" }],
      // Lets the canvas test drive the expectation step on an assessed link.
      accepts_expectation: true,
    },
    {
      name: "contradicts",
      source_types: ["evidence", "hypothesis"],
      target_types: ["hypothesis", "evidence"],
      same_type: false,
      assessment_required_for: [],
      accepts_expectation: false,
    },
    {
      name: "relates_to",
      source_types: ["hypothesis"],
      target_types: ["hypothesis"],
      same_type: false,
      assessment_required_for: [],
      accepts_expectation: false,
    },
  ],
};
type Snapshot = { graph: GraphState; draft: HumanDraft };
function addEdge(snapshot: Snapshot, edge: NewEdge) {
  snapshot.graph.edges[edge.id] = { ...edge, layer: "epistemic" };
  snapshot.draft.added_edges.push(edge);
}
function removeEdge(snapshot: Snapshot, id: string) {
  delete snapshot.graph.edges[id];
  snapshot.draft.added_edges = snapshot.draft.added_edges.filter((edge) => edge.id !== id);
  if (graph.edges[id]) snapshot.draft.removed_edge_ids.push(id);
}
function removeNode(snapshot: Snapshot, id: string) {
  delete snapshot.graph.nodes[id];
  delete snapshot.draft.custom_nodes[id];
  if (graph.nodes[id]) snapshot.draft.removed_node_ids.push(id);
  Object.values(snapshot.graph.edges)
    .filter((edge) => edge.source === id || edge.target === id)
    .forEach((edge) => removeEdge(snapshot, edge.id));
}
function addNode(snapshot: Snapshot, node: NewNode) {
  snapshot.draft.custom_nodes[node.id] = node;
  snapshot.graph.nodes[node.id] = {
    ...node,
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
  } as GraphNode;
}

function Fixture() {
  const [snapshot, setSnapshot] = useState<Snapshot>({ graph, draft: emptyHumanDraft(1) });
  const [preview, setPreview] = useState(graph);
  const [calls, setCalls] = useState<Array<{ method: string; args: unknown[] }>>([]);
  const [disabled, setDisabled] = useState(false);
  const [hold, setHold] = useState(false);
  const undo = useRef<Snapshot[]>([]);
  const redo = useRef<Snapshot[]>([]);
  const viewport = useRef(null);
  function record(method: string, ...args: unknown[]) {
    setCalls((items) => [...items, { method, args }]);
  }
  function update(method: string, args: unknown[], mutate: (next: Snapshot) => void) {
    record(method, ...args);
    const next = structuredClone(snapshot);
    mutate(next);
    undo.current.push(snapshot);
    redo.current = [];
    setSnapshot(next);
    if (!hold) setPreview(next.graph);
  }
  function travel(method: "onUndo" | "onRedo") {
    record(method);
    const source = method === "onUndo" ? undo : redo;
    const target = method === "onUndo" ? redo : undo;
    const next = source.current.pop();
    if (!next) return;
    target.current.push(snapshot);
    setSnapshot(next);
    if (!hold) setPreview(next.graph);
  }
  const editing: GraphEditingProps = {
    projectId: "fixture",
    mutationsDisabled: disabled,
    editOptions,
    canonicalGraph: graph,
    humanDraft: snapshot.draft,
    activeExperimentIds: [],
    canUndo: undo.current.length > 0,
    canRedo: redo.current.length > 0,
    onStageNode: (node, edges) =>
      update("onStageNode", [node, edges], (next) => {
        addNode(next, node);
        edges.forEach((edge) => addEdge(next, edge));
      }),
    onUpdateStagedNode: (node) =>
      update("onUpdateStagedNode", [node], (next) => addNode(next, node)),
    onStageEdge: (edge) => update("onStageEdge", [edge], (next) => addEdge(next, edge)),
    onReplaceEdge: (id, edge) =>
      update("onReplaceEdge", [id, edge], (next) => {
        removeEdge(next, id);
        addEdge(next, edge);
      }),
    onRemoveEdge: (id) => update("onRemoveEdge", [id], (next) => removeEdge(next, id)),
    onRemoveNodes: (ids) =>
      update("onRemoveNodes", [ids], (next) => ids.forEach((id) => removeNode(next, id))),
    onMergeNodes: (duplicateId, canonicalId) =>
      update("onMergeNodes", [duplicateId, canonicalId], (next) => {
        Object.values(next.graph.edges).forEach((edge) => {
          if (edge.source !== duplicateId && edge.target !== duplicateId) return;
          const source = edge.source === duplicateId ? canonicalId : edge.source;
          const target = edge.target === duplicateId ? canonicalId : edge.target;
          if (
            source === target ||
            Object.values(next.graph.edges).some(
              (existing) =>
                existing.source === source &&
                existing.target === target &&
                existing.relation === edge.relation,
            )
          )
            return;
          addEdge(next, { ...edge, id: `${edge.id}/merged`, source, target });
        });
        removeNode(next, duplicateId);
      }),
    onRenameNode: (id, title) =>
      update("onRenameNode", [id, title], (next) => {
        next.graph.nodes[id].title = title;
        if (next.draft.custom_nodes[id]) next.draft.custom_nodes[id].title = title;
        else next.draft.nodes[id] = { base_updated_rev: 1, changes: { title } };
      }),
    onUndo: () => travel("onUndo"),
    onRedo: () => travel("onRedo"),
  };
  return (
    <>
      <button onClick={() => setDisabled(!disabled)}>Toggle read-only</button>
      <label>
        <input
          type="checkbox"
          checked={hold}
          onChange={(event) => {
            setHold(event.target.checked);
            if (!event.target.checked) setPreview(snapshot.graph);
          }}
        />
        Hold preview
      </label>
      <DagView
        {...editing}
        graph={preview}
        trustView="working"
        viewportRef={viewport}
        onSelectNode={(node) => record("onSelectNode", node.id)}
      />
      <output hidden aria-label="Canvas calls">
        {JSON.stringify(calls)}
      </output>
      <output hidden aria-label="Staged request">
        {JSON.stringify(snapshot.draft)}
      </output>
    </>
  );
}
createRoot(document.getElementById("root")!).render(<Fixture />);
