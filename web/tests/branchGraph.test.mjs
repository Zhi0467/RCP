import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const { graphTargetUrl, graphSessionKey, graphTargetFromHash, graphViewHash } =
  await server.ssrLoadModule("/src/graphTarget.ts");
const { parseProjectHash, projectHashAfterViewChange } =
  await server.ssrLoadModule("/src/experimentBoard.ts");
const { branchGraphProjection, expandBranchContext } =
  await server.ssrLoadModule("/src/branchGraph.ts");
const {
  emptyProjectSessionState,
  projectSessionReducer,
  serializeProjectSessionTabState,
  persistProjectHumanDraft,
} = await server.ssrLoadModule("/src/hooks/projectSession.ts");
const { emptyHumanDraft, stageNodeEdit } = await server.ssrLoadModule("/src/humanDraft.ts");
const { transitionSyncCompletionDisposition } = await server.ssrLoadModule(
  "/src/projectTransition.ts",
);
const { loadChatSummaryPage, loadChatTranscript } = await server.ssrLoadModule("/src/chatApi.ts");

const main = { kind: "main" };
const branch = { kind: "branch", branch_id: "episode-branch" };
const head = (target, revision) => ({
  target,
  revision,
  transition_id: `transition-${target.kind}-${revision}`,
});
const node = (id, title = id) => ({
  id,
  title,
  type: "hypothesis",
  statement: title,
  standing: "asserted",
  created_rev: 1,
  updated_rev: 1,
  source_refs: [],
  extension_fields: {},
});
const before = node("hyp/changed", "Original claim");
const afterNode = node("hyp/changed", "New claim");
const removed = node("hyp/removed");
const graph = {
  revision: 2,
  nodes: { [afterNode.id]: afterNode, context: node("context"), distant: node("distant") },
  edges: {
    first: {
      id: "first",
      source: afterNode.id,
      target: "context",
      relation: "relates_to",
      explanation: "",
      layer: "meta",
    },
    second: {
      id: "second",
      source: "context",
      target: "distant",
      relation: "relates_to",
      explanation: "",
      layer: "meta",
    },
  },
  proposals: {},
  glossary: {},
  ontology: { types: [], fields: [], relations: [] },
  ambiguities: {},
  validation_messages: [],
  belief_transitions: [],
  replay_status: "complete",
  replay_failure: null,
};
const changes = {
  branch_id: branch.branch_id,
  base_head: head(main, 5),
  head: head(branch, 2),
  nodes: [
    { node_id: before.id, change: "updated", before, after: afterNode, history: [] },
    { node_id: removed.id, change: "removed", before: removed, after: null, history: [] },
  ],
  edges: [],
  changed_node_ids: [before.id, removed.id],
  context_node_ids: ["context"],
};
function snapshot(target, revision) {
  return {
    id: "project",
    graph_target: target,
    graph_head: head(target, revision),
    graph_changes: target.kind === "branch" ? { ...changes, head: head(target, revision) } : null,
    graph: { ...graph, revision },
    revision,
    snapshot_freshness: "fresh",
    attention: {
      pending_proposal_ids: [],
      open_blocker_ids: [],
      decisions_awaiting_choice_ids: [],
      proposal_actions: {},
      decision_prior_choices: {},
    },
  };
}
function apply(state, next) {
  return projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: next,
    preserve_readiness: false,
  });
}

test("graph navigation retains the branch through ordinary views and main is explicit", () => {
  for (const view of ["dag", "scientific", "attention", "chats", "execution"]) {
    const hash = graphViewHash("project", branch, view);
    assert.deepEqual(graphTargetFromHash(hash), branch);
    assert.equal(parseProjectHash(hash).view, view);
    assert.deepEqual(graphTargetFromHash(projectHashAfterViewChange(hash, "chats")), branch);
  }
  assert.deepEqual(graphTargetFromHash(graphViewHash("project", main)), main);
  assert.deepEqual(graphTargetFromHash("#/projects/project?branch_id="), {
    kind: "branch",
    branch_id: "",
  });
  assert.equal(
    graphTargetUrl("/api/projects/project/sync?preview=1", branch),
    "/api/projects/project/sync?preview=1&branch_id=episode-branch",
  );
});

test("a branch lens includes changes and context without adding removed nodes to editable graph", () => {
  const projected = branchGraphProjection(graph, changes, new Set());
  assert.deepEqual(Object.keys(projected.nodes).sort(), [before.id, removed.id, "context"].sort());
  assert.equal(graph.nodes[removed.id], undefined);
  assert.equal(projected.nodes[removed.id], removed);
  const expanded = expandBranchContext(graph, new Set(Object.keys(projected.nodes)));
  assert.ok(branchGraphProjection(graph, changes, expanded).nodes.distant);
  assert.ok(branchGraphProjection(graph, changes, null).nodes.distant);
});

test("switching graph targets isolates snapshots, local drafts, and an in-flight Sync", () => {
  let state = apply(emptyProjectSessionState("project", main), snapshot(main, 5));
  const mainDraft = stageNodeEdit(emptyHumanDraft(5), state.project.graph, afterNode.id, {
    title: "Main draft",
  });
  state = projectSessionReducer(state, {
    kind: "human_draft_updated",
    project_id: "project",
    draft: mainDraft,
  });
  const savedMain = serializeProjectSessionTabState(state);
  const fence = {
    project_id: "project",
    request_id: 1,
    expected_head: head(main, 5),
    draft_generation: 1,
  };
  state = projectSessionReducer(state, {
    kind: "sync_started",
    fence,
    snapshot_request_id: 1,
    sync_request_sequence: 1,
  });
  state = projectSessionReducer(state, {
    kind: "reset",
    project_id: "project",
    graph_target: branch,
  });
  assert.equal(
    transitionSyncCompletionDisposition(state.transitionCoordinator, fence),
    "reload_inactive",
  );
  assert.equal(apply(state, snapshot(main, 9)), state);
  state = apply(state, snapshot(branch, 2));
  assert.equal(state.renderedRevision, 2);
  assert.equal(state.humanDraft, null);
  assert.deepEqual(state.transitionHead, head(branch, 2));
  const branchDraft = stageNodeEdit(emptyHumanDraft(2), state.project.graph, afterNode.id, {
    title: "Branch draft",
  });
  const stored = new Map();
  persistProjectHumanDraft(
    { setItem: (key, value) => stored.set(key, value), removeItem: (key) => stored.delete(key) },
    "project",
    mainDraft,
    main,
  );
  persistProjectHumanDraft(
    { setItem: (key, value) => stored.set(key, value), removeItem: (key) => stored.delete(key) },
    "project",
    branchDraft,
    branch,
  );
  assert.equal(stored.size, 2);
  assert.notEqual(graphSessionKey("project", main), graphSessionKey("project", branch));
  state = projectSessionReducer(state, {
    kind: "restore_tab",
    project_id: "project",
    state: savedMain,
  });
  assert.equal(state.humanDraft.nodes[afterNode.id].changes.title, "Main draft");
  assert.deepEqual(state.graphTarget, main);
});

test("branch snapshots and deltas must carry the same target and head", () => {
  const state = emptyProjectSessionState("project", branch);
  const wrongHead = { ...snapshot(branch, 2), graph_head: head(main, 2) };
  assert.equal(apply(state, wrongHead), state);
  const oldDelta = { ...snapshot(branch, 2), graph_changes: { ...changes, head: head(branch, 1) } };
  assert.equal(apply(state, oldDelta), state);
  const otherTransition = {
    ...snapshot(branch, 2),
    graph_changes: { ...changes, head: { ...head(branch, 2), transition_id: "other-transition" } },
  };
  assert.equal(apply(state, otherTransition), state);
});

test("branch conversation reads reject cross-target responses", async () => {
  const calls = [];
  const page = { items: [{ chat_id: "chat", graph_target: branch }], offset: 0, total: 1 };
  await loadChatSummaryPage(
    "/api/projects/project",
    0,
    async (url) => {
      calls.push(url);
      return page;
    },
    branch,
  );
  await loadChatTranscript(
    "/api/projects/project",
    "chat",
    async (url) => {
      calls.push(url);
      return page.items[0];
    },
    branch,
  );
  assert.ok(calls.every((url) => url.includes("branch_id=episode-branch")));
  await assert.rejects(
    loadChatTranscript(
      "/api/projects/project",
      "chat",
      async () => ({ graph_target: main }),
      branch,
    ),
    /different graph target/,
  );
  await assert.rejects(
    loadChatSummaryPage(
      "/api/projects/project",
      0,
      async () => ({ ...page, items: [{ graph_target: main }] }),
      branch,
    ),
    /different graph target/,
  );
});

test("ordinary local drafts remain visible after restoring a branch session", async () => {
  const { attentionGraphForProjection } = await server.ssrLoadModule("/src/App.tsx");
  const draft = stageNodeEdit(emptyHumanDraft(graph.revision), graph, afterNode.id, {
    title: "Human review",
  });
  const displayed = attentionGraphForProjection(graph, null, "local_draft", draft);
  assert.equal(displayed.nodes[afterNode.id].title, "Human review");
  assert.equal(graph.nodes[afterNode.id].title, "New claim");
});

test("merge review decodes one same-node content, status and standing bundle", async () => {
  const { decodeProposal, proposalSemantics } = await server.ssrLoadModule("/src/types.ts");
  const ops = [
    {
      op: "update_nodes",
      intent: "content_change",
      nodes: [{ id: "hyp/one", changes: { statement: "Reviewed statement" } }],
    },
    {
      op: "update_nodes",
      intent: "status_change",
      nodes: [{ id: "hyp/one", changes: { status: "supported" }, cause: { kind: "human_edit" } }],
    },
    { op: "set_standing", intent: "standing_change", node_id: "hyp/one", standing: "accepted" },
  ];
  const proposal = decodeProposal({ id: "proposal", ops });
  assert.equal(proposal.semantics, "canonical");
  assert.equal(proposal.ops.length, 3);
  assert.deepEqual(proposalSemantics(proposal).resourceKeys, ["node:hyp/one"]);
  assert.equal(decodeProposal({ ops: [ops[0], ops[0]] }).semantics, "legacy");
  assert.equal(
    decodeProposal({ ops: [ops[0], { ...ops[2], node_id: "hyp/other" }] }).semantics,
    "legacy",
  );
});
