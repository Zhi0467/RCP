import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { DagView } from "../../src/views/GraphViews";
import { DetailDrawer } from "../../src/components/DetailDrawer";
import { NodeChat } from "../../src/components/NodeChat";
import { api } from "../../src/api";
import { graphTargetUrl } from "../../src/graphTarget";
import {
  applyHumanDraft,
  emptyHumanDraft,
  stageNodeEdit,
  stageCustomNode,
  stageEdgeAddition,
  stageEdgeRemoval,
  unstageEdgeRemoval,
  toHumanSyncRequest,
} from "../../src/humanDraft";
import { buildGlossaryIndex } from "../../src/glossary";
import type {
  AgentTask,
  GraphBranchChanges,
  GraphNode,
  GraphState,
  ProjectSnapshot,
} from "../../src/types";
import "../../src/styles.css";

const target = { kind: "branch", branch_id: "episode-branch" } as const;
const makeNode = (id: string, title: string) =>
  ({
    id,
    title,
    type: "hypothesis",
    statement: title,
    standing: "asserted",
    status: "proposed",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  }) as GraphNode;
const original = makeNode("hyp/changed", "Original claim");
const updated = makeNode("hyp/changed", "Revised claim");
const removed = makeNode("hyp/removed", "Removed claim");
const nodes = [
  updated,
  makeNode("hyp/new", "New claim"),
  makeNode("hyp/context", "Context claim"),
  makeNode("hyp/distant", "Distant claim"),
];
const graph = {
  revision: 2,
  nodes: Object.fromEntries(nodes.map((node) => [node.id, node])),
  edges: {
    first: {
      id: "first",
      source: updated.id,
      target: "hyp/context",
      relation: "relates_to",
      explanation: "Direct context",
      layer: "meta",
    },
    second: {
      id: "second",
      source: "hyp/context",
      target: "hyp/distant",
      relation: "relates_to",
      explanation: "More context",
      layer: "meta",
    },
  },
  proposals: {},
  glossary: {},
  ambiguities: {},
  ontology: { types: [], fields: [], relations: [] },
  validation_messages: [],
  belief_transitions: [],
  replay_status: "complete",
  replay_failure: null,
} as GraphState;
const history = [
  {
    revision: 2,
    producer: "agent",
    summary: "Compared the baseline",
    task_id: "worker-task",
    episode_id: "episode",
  },
] as const;
const changes: GraphBranchChanges = {
  branch_id: target.branch_id,
  base_head: { target: { kind: "main" }, revision: 7, transition_id: null },
  head: { target, revision: 2, transition_id: "branch-two" },
  nodes: [
    {
      node_id: original.id,
      change: "updated",
      before: original,
      after: updated,
      history: [...history],
    },
    {
      node_id: "hyp/new",
      change: "created",
      before: null,
      after: graph.nodes["hyp/new"],
      history: [...history],
    },
    {
      node_id: removed.id,
      change: "removed",
      before: removed,
      after: null,
      history: [
        {
          revision: 1,
          producer: "human",
          summary: "Removed duplicate claim",
          task_id: null,
          episode_id: null,
        },
      ],
    },
  ],
  edges: [],
  changed_node_ids: [updated.id, "hyp/new", removed.id],
  context_node_ids: ["hyp/context"],
};
const profile = {
  provider: "codex",
  model: "",
  reasoning: "medium",
  run_on: "local",
  permissions: {},
};
const project = {
  id: "branch-fixture",
  name: "Branch fixture",
  graph_target: target,
  graph_head: changes.head,
  graph_changes: changes,
  graph,
  agent_profiles: { node_chat: profile, project_chat: profile },
  provider_readiness: {
    local: {
      codex: {
        provider: "codex",
        label: "Codex",
        installed: true,
        authenticated: true,
        models: [],
      },
    },
  },
  repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
  project_truth_scope: ["repo"],
  state_repository: "repo",
  machines: [{ alias: "local", host: null }],
} as unknown as ProjectSnapshot;

function Fixture() {
  const viewport = useRef(null);
  const [draft, setDraft] = useState(emptyHumanDraft(graph.revision));
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [chat, setChat] = useState<GraphNode | null>(null);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [inspectedTask, setInspectedTask] = useState("");
  const presented = applyHumanDraft(graph, draft);
  const historical = selected !== null && !graph.nodes[selected.id];
  const selectedNode = selected ? (presented.nodes[selected.id] ?? selected) : null;
  return (
    <main style={{ padding: 24 }}>
      <DagView
        projectId={project.id}
        graphTarget={target}
        branchChanges={changes}
        graph={presented}
        trustView="review"
        viewportRef={viewport}
        onSelectNode={setSelected}
        onInspectTask={setInspectedTask}
        onStageCustomNode={(node) => setDraft((current) => stageCustomNode(current, node))}
        onStageEdge={(edge) => setDraft((current) => stageEdgeAddition(current, graph, edge))}
        onRemoveEdge={(edgeId) => setDraft((current) => stageEdgeRemoval(current, graph, edgeId))}
        onUndoRemoveEdge={(edgeId) =>
          setDraft((current) => unstageEdgeRemoval(current, graph, edgeId))
        }
        canonicalEdges={graph.edges}
        draftAddedNodeIds={Object.keys(draft.custom_nodes)}
        draftAddedEdges={draft.added_edges}
        draftRemovedEdgeIds={draft.removed_edge_ids}
      />
      {selectedNode && (
        <DetailDrawer
          node={selectedNode}
          branchChange={changes.nodes.find((change) => change.node_id === selectedNode.id)}
          historical={historical}
          onInspectTask={setInspectedTask}
          edges={Object.values(graph.edges)}
          allNodes={graph.nodes}
          glossaryIndex={buildGlossaryIndex({})}
          beliefTransitions={[]}
          validationMessages={[]}
          ontology={graph.ontology}
          detailSlot="original"
          canonicalNode={graph.nodes[selectedNode.id]}
          onClose={() => setSelected(null)}
          onDock={() => setSelected(null)}
          onBeginEdit={() => {}}
          onStanding={() => {}}
          onStage={(fields) =>
            setDraft((current) => stageNodeEdit(current, graph, selectedNode.id, fields))
          }
          onOpenChat={() => {
            setChat(selectedNode);
            setSelected(null);
          }}
          onOpenRelatedNode={() => {}}
          onSelectNode={() => {}}
        />
      )}
      {chat && (
        <div
          style={{
            position: "fixed",
            inset: "20px 20px 20px 35%",
            zIndex: 40,
            background: "var(--sheet)",
            border: "1px solid var(--rule)",
          }}
        >
          <NodeChat
            project={project}
            node={chat}
            nodes={graph.nodes}
            runScope={["repo"]}
            tasks={tasks}
            chatId="ordinary-review-chat"
            presentation="workspace"
            onStartTask={async (kind, request) => {
              const task = await api<AgentTask>(
                graphTargetUrl(`/api/projects/${project.id}/tasks/${kind}`, target),
                { method: "POST", body: JSON.stringify(request) },
              );
              setTasks((current) => [...current, task]);
              return task;
            }}
            onInspectTask={setInspectedTask}
            onOpenInbox={() => {}}
            onRepairGraphUpdate={async () => {}}
            onNewSession={() => {}}
            onClose={() => setChat(null)}
            onResumeTask={() => {}}
            onRetryTask={() => {}}
            onRefreshTask={async () => tasks[0]}
          />
        </div>
      )}
      <output aria-label="Staged request">
        {JSON.stringify(toHumanSyncRequest(draft, graph))}
      </output>
      <output aria-label="Inspected task">{inspectedTask}</output>
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<Fixture />);
