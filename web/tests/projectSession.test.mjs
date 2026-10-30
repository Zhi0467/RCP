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
const {
  emptyProjectSessionState,
  projectDraftPreviewEffectInputs,
  projectSessionReducer,
  projectSettingsSavedProject,
  serializeProjectSessionTabState,
  trustedProjectTransitionManifest,
} = await server.ssrLoadModule("/src/graph/projectSession.ts");
const { transitionSyncCompletionDisposition } = await server.ssrLoadModule(
  "/src/graph/projectTransition.ts",
);

after(() => server.close());

const transitionOne = "1".repeat(64);
const transitionTwo = "2".repeat(64);

function graph(revision, node = null) {
  return {
    revision,
    nodes: node ? { [node.id]: node } : {},
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
}

function snapshot(revision, fields = {}) {
  return {
    id: "alpha",
    name: "Alpha",
    revision,
    snapshot_freshness: "fresh",
    graph: graph(revision),
    attention: {
      pending_proposal_ids: [],
      decisions_awaiting_choice_ids: [],
      open_blocker_ids: [],
      proposal_actions: {},
      stale_proposal_ids: [],
      refreshable_proposal_ids: [],
      stale_edge_ids: [],
      decision_prior_choices: {},
    },
    ...fields,
  };
}

function projection(revision) {
  const transitionId = revision === 1 ? transitionOne : transitionTwo;
  return {
    head: { target: { kind: "main" }, revision, transition_id: transitionId },
    graph: graph(revision),
    attention: {
      pending_proposal_ids: [],
      decisions_awaiting_choice_ids: [],
      open_blocker_ids: [],
      proposal_actions: {},
      stale_proposal_ids: [],
      refreshable_proposal_ids: [],
      stale_edge_ids: [],
      decision_prior_choices: {},
    },
    primary_question: null,
    counts: {
      pending_proposals: 0,
      decisions_awaiting_choice: 0,
      open_blockers: 0,
      asserted: 0,
      accepted: 0,
      contested: 0,
    },
    experiment_control: {},
    ruleset_tag: "rcp.lifecycle.v1",
    transition_id: transitionId,
    canonical: false,
    base_head: { target: { kind: "main" }, revision: revision - 1, transition_id: null },
  };
}

function humanDraft(revision, title = "My staged title") {
  return {
    version: 1,
    base_revision: revision,
    nodes: {
      "hyp/example": {
        base_updated_rev: revision,
        changes: { title },
        standing: "asserted",
        standing_origin: "edit",
      },
    },
    removed_node_ids: [],
    proposals: {},
    ontology: null,
    custom_nodes: {},
    added_edges: [],
    removed_edge_ids: [],
    edge_base_revision: null,
  };
}

test("snapshot application ignores a stale request generation", () => {
  let state = emptyProjectSessionState("alpha");
  state = projectSessionReducer(state, {
    kind: "snapshot_request_started",
    project_id: "alpha",
    request_id: 1,
  });
  state = projectSessionReducer(state, {
    kind: "snapshot_request_started",
    project_id: "alpha",
    request_id: 2,
  });

  const ignored = projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
    request: { project_id: "alpha", request_id: 1 },
  });

  assert.strictEqual(ignored, state);
  assert.equal(ignored.project, null);
});

test("an older revision never replaces the newer rendered session", () => {
  const current = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(2),
    preserve_readiness: false,
  });
  const ignored = projectSessionReducer(current, {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });

  assert.strictEqual(ignored, current);
  assert.equal(ignored.renderedRevision, 2);
  assert.equal(ignored.project.graph.revision, 2);
});

test("snapshot movement invalidates the manifest and rebases the draft in one transition", () => {
  const originalNode = {
    id: "hyp/example",
    type: "hypothesis",
    title: "Canonical before",
    statement: "Statement",
    standing: "accepted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  };
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1, { graph: graph(1, originalNode) }),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, {
    kind: "human_draft_loaded",
    draft: {
      version: 1,
      base_revision: 1,
      nodes: {
        [originalNode.id]: {
          base_updated_rev: 1,
          changes: { title: "My staged title" },
          standing: "asserted",
          standing_origin: "edit",
        },
      },
      removed_node_ids: [],
      proposals: {},
      ontology: null,
      custom_nodes: {},
      added_edges: [],
      removed_edge_ids: [],
      edge_base_revision: null,
    },
  });
  state = projectSessionReducer(state, {
    kind: "manifest_valid",
    project_id: "alpha",
    manifest: { ruleset_tag: "rcp.lifecycle.v1", triggers: [] },
  });
  state = projectSessionReducer(state, {
    kind: "draft_preview_changed",
    projection: projection(2),
    conflict: null,
    pending: false,
  });
  const beforeRefresh = state.transitionManifestRefresh;
  const movedNode = { ...originalNode, title: "Canonical after", updated_rev: 2 };

  const moved = projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: snapshot(2, { graph: graph(2, movedNode) }),
    preserve_readiness: false,
  });

  assert.equal(moved.project.graph.revision, 2);
  assert.equal(moved.renderedRevision, 2);
  assert.equal(moved.humanDraft.base_revision, 2);
  assert.equal(moved.humanDraft.nodes[originalNode.id].base_updated_rev, 1);
  assert.equal(moved.transitionManifestState.status, "loading");
  assert.equal(moved.transitionManifestRefresh, beforeRefresh + 1);
  assert.equal(moved.draftTransitionProjection, null);
  assert.equal(moved.draftPreviewConflict, null);
  assert.equal(moved.draftPreviewPending, false);
});

test("re-applying an unchanged snapshot keeps the staged draft and its preview", () => {
  const originalNode = {
    id: "hyp/example",
    type: "hypothesis",
    title: "Canonical",
    statement: "Statement",
    standing: "accepted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  };
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1, { graph: graph(1, originalNode) }),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, {
    kind: "human_draft_loaded",
    draft: {
      version: 1,
      base_revision: 1,
      nodes: {},
      removed_node_ids: [originalNode.id],
      proposals: {},
      ontology: null,
      custom_nodes: {},
      added_edges: [],
      removed_edge_ids: [],
      edge_base_revision: null,
    },
  });
  state = projectSessionReducer(state, {
    kind: "draft_preview_changed",
    projection: projection(2),
    conflict: null,
    pending: false,
  });
  const stagedDraft = state.humanDraft;
  const stagedProjection = state.draftTransitionProjection;
  const stagedHead = state.transitionHead;

  // Live watcher-delivery polling re-applies the same canonical snapshot every
  // few seconds. It must not restart the staged preview, or the view flips
  // between canonical and candidate for as long as a watcher is undelivered.
  const repolled = projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: snapshot(1, { graph: graph(1, originalNode) }),
    preserve_readiness: false,
  });

  assert.strictEqual(repolled.humanDraft, stagedDraft);
  assert.strictEqual(repolled.draftTransitionProjection, stagedProjection);
  assert.strictEqual(repolled.transitionHead, stagedHead);
  assert.equal(repolled.draftPreviewPending, false);

  // Operational state that really did move at the same graph revision is still
  // applied: that poll exists to deliver watcher and control state, and holding
  // a stale control projection would strand Runs on "waiting" indefinitely.
  // Presentation prefers the projection's own control map, so the projection is
  // dropped rather than retained beside fresh state, and the effects refetch it.
  const controlMoved = projectSessionReducer(repolled, {
    kind: "snapshot_applied",
    snapshot: snapshot(1, {
      graph: graph(1, originalNode),
      experiment_control: { "exp/one": { health: "completed" } },
    }),
    preserve_readiness: false,
  });

  assert.deepEqual(controlMoved.project.experiment_control, {
    "exp/one": { health: "completed" },
  });
  assert.equal(controlMoved.draftTransitionProjection, null);
  assert.strictEqual(controlMoved.humanDraft, stagedDraft);
  assert.strictEqual(controlMoved.project.graph, repolled.project.graph);
});

test("a repoll of the head a preview built on keeps the staged preview", () => {
  const question = {
    id: "rq/example",
    type: "research_question",
    title: "Question",
    statement: "Statement",
    standing: "accepted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  };
  const hypothesis = { ...question, id: "hyp/example", type: "hypothesis", title: "Canonical" };
  const exactHead = { target: { kind: "main" }, revision: 1, transition_id: transitionOne };
  const polled = (syncedAt) =>
    snapshot(1, {
      graph: graph(1, hypothesis),
      // A display cache written before snapshots named the transition id.
      graph_head: { target: { kind: "main" }, revision: 1, transition_id: null },
      // Each poll parses fresh JSON, so no object is shared between reads.
      primary_question: structuredClone(question),
      // Team and SSH-backed projects restamp this on every read.
      last_remote_sync_at: syncedAt,
    });
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: polled("2026-09-27T20:00:00Z"),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, { kind: "human_draft_loaded", draft: humanDraft(1) });
  state = projectSessionReducer(state, {
    kind: "preview_applied",
    project_id: "alpha",
    projection: { ...projection(2), base_head: exactHead },
    base_head: exactHead,
  });

  const repolled = projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: polled("2026-09-27T20:00:06Z"),
    preserve_readiness: false,
  });

  assert.strictEqual(repolled.draftTransitionProjection, state.draftTransitionProjection);
  assert.deepEqual(repolled.transitionHead, exactHead);
});

test("an unchanged snapshot keeps preview status, and a moved one clears it", () => {
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, {
    kind: "draft_preview_changed",
    projection: null,
    conflict: "Staged transition preview was refused: stale base head.",
    pending: false,
  });

  // Sync is gated on this conflict. Clearing it on a watcher-delivery poll would
  // re-enable Sync for an edit the backend already refused, and the preview
  // effects no longer rerun to restore it.
  const repolled = projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });
  assert.equal(repolled.draftPreviewConflict, state.draftPreviewConflict);

  const moved = projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: snapshot(2),
    preserve_readiness: false,
  });
  assert.equal(moved.draftPreviewConflict, null);
  assert.equal(moved.draftPreviewPending, false);
});

test("a committed transition replaces the canonical session in one transition", () => {
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, {
    kind: "manifest_valid",
    project_id: "alpha",
    manifest: { ruleset_tag: "rcp.lifecycle.v1", triggers: [] },
  });
  state = projectSessionReducer(state, {
    kind: "draft_preview_changed",
    projection: projection(2),
    conflict: "old preview",
    pending: true,
  });
  const committed = {
    ...projection(2),
    ruleset_tag: "rcp.lifecycle.v2",
    canonical: true,
  };

  const applied = projectSessionReducer(state, {
    kind: "committed_transition_applied",
    project_id: "alpha",
    projection: committed,
    submitted_draft: {
      version: 1,
      base_revision: 1,
      nodes: {},
      removed_node_ids: [],
      proposals: {},
      ontology: null,
      custom_nodes: {},
      added_edges: [],
      removed_edge_ids: [],
      edge_base_revision: null,
    },
  });

  assert.equal(applied.project.graph.revision, 2);
  assert.equal(applied.renderedRevision, 2);
  assert.deepEqual(applied.transitionHead, committed.head);
  assert.equal(applied.transitionRulesetTag, "rcp.lifecycle.v2");
  assert.equal(applied.transitionManifestState.status, "loading");
  assert.equal(applied.transitionManifestExpectedRulesetTag, "rcp.lifecycle.v2");
  assert.equal(applied.humanDraft, null);
  assert.equal(applied.draftTransitionProjection, null);
  assert.equal(applied.draftPreviewConflict, null);
  assert.equal(applied.draftPreviewPending, false);
});

test("a loading stale manifest cannot invalidate a committed transition", () => {
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, {
    kind: "manifest_valid",
    project_id: "alpha",
    manifest: { ruleset_tag: "rcp.lifecycle.v1", triggers: [] },
  });
  state = projectSessionReducer(state, {
    kind: "manifest_loading",
    project_id: "alpha",
    manifest: { ruleset_tag: "rcp.lifecycle.stale", triggers: [] },
  });
  const manifestState = state.transitionManifestState;
  const manifestRefresh = state.transitionManifestRefresh;

  assert.equal(trustedProjectTransitionManifest(state, "alpha"), null);
  const applied = projectSessionReducer(state, {
    kind: "committed_transition_applied",
    project_id: "alpha",
    projection: { ...projection(2), canonical: true },
    submitted_draft: humanDraft(1),
  });

  assert.strictEqual(applied.transitionManifestState, manifestState);
  assert.equal(applied.transitionManifestExpectedRulesetTag, null);
  assert.equal(applied.transitionManifestRefresh, manifestRefresh);
});

test("a settings save replaces project metadata without reconciling session state", () => {
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, {
    kind: "human_draft_loaded",
    draft: humanDraft(1),
  });
  state = projectSessionReducer(state, {
    kind: "draft_preview_changed",
    projection: projection(2),
    conflict: "retained preview conflict",
    pending: true,
  });
  state = { ...state, draftReconciliationDiscardedProposalIds: ["proposal/retained"] };
  const previousDraft = state.humanDraft;
  const previousProjection = state.draftTransitionProjection;

  const saved = projectSettingsSavedProject(snapshot(1, { name: "Renamed Alpha" }), state.project, {
    provider: true,
  });
  const updated = projectSessionReducer(state, { kind: "project_replaced", project: saved });

  assert.equal(updated.project.name, "Renamed Alpha");
  assert.strictEqual(updated.humanDraft, previousDraft);
  assert.strictEqual(updated.draftTransitionProjection, previousProjection);
  assert.equal(updated.draftPreviewConflict, "retained preview conflict");
  assert.equal(updated.draftPreviewPending, true);
  assert.deepEqual(updated.draftReconciliationDiscardedProposalIds, ["proposal/retained"]);
});

test("a machine change takes fresh provider readiness; a settings save keeps the rendered one", () => {
  const readiness = { provider: "codex", installed: true, authenticated: true, version: "1" };
  const current = snapshot(1, {
    provider_readiness: { laptop: { codex: { ...readiness, installed: false, version: "0" } } },
  });
  const saved = snapshot(1, { provider_readiness: { laptop: { codex: readiness } } });

  assert.deepEqual(
    projectSettingsSavedProject(saved, current, { provider: false }).provider_readiness,
    saved.provider_readiness,
  );
  assert.deepEqual(
    projectSettingsSavedProject(saved, current, { provider: true }).provider_readiness,
    current.provider_readiness,
  );
});

test("tab serialization retains only human UI and drafts", () => {
  const state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(2),
    preserve_readiness: false,
  });
  const saved = serializeProjectSessionTabState(state);
  assert.deepEqual(
    Object.keys(saved).sort(),
    ["projectId", "graphTarget", "humanDraft", "draftReconciliationDiscardedProposalIds"].sort(),
  );
  assert.equal(saved.project, undefined);
});

test("reset replaces project identity and a stored draft in one reducer transition", () => {
  const projectY = projectSessionReducer(emptyProjectSessionState("project-y"), {
    kind: "human_draft_loaded",
    draft: humanDraft(7, "Project Y draft"),
  });
  const projectXDraft = humanDraft(0, "Project X stored draft");

  const reset = projectSessionReducer(projectY, {
    kind: "reset",
    project_id: "project-x",
    human_draft: projectXDraft,
  });

  assert.equal(reset.projectId, "project-x");
  assert.strictEqual(reset.humanDraft, projectXDraft);
});

test("a Sync response becomes inactive after its project tab is replaced", () => {
  const projectXHead = { target: { kind: "main" }, revision: 1, transition_id: transitionOne };
  const fence = {
    project_id: "project-x",
    request_id: 1,
    expected_head: projectXHead,
    draft_generation: 0,
  };
  let projectX = projectSessionReducer(emptyProjectSessionState("project-x"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1, { id: "project-x", name: "Project X" }),
    preserve_readiness: false,
  });
  projectX = projectSessionReducer(projectX, {
    kind: "sync_started",
    fence,
    snapshot_request_id: 1,
    sync_request_sequence: 1,
  });
  const projectY = projectSessionReducer(emptyProjectSessionState("project-y"), {
    kind: "snapshot_applied",
    snapshot: snapshot(2, { id: "project-y", name: "Project Y" }),
    preserve_readiness: false,
  });

  const switched = projectSessionReducer(projectX, {
    kind: "restore_tab",
    project_id: "project-y",
    state: serializeProjectSessionTabState(projectY),
  });

  assert.equal(switched.transitionCoordinator.active_project_id, "project-y");
  assert.equal(
    transitionSyncCompletionDisposition(switched.transitionCoordinator, fence),
    "reload_inactive",
  );
});

test("an empty project session survives tab serialization and restoration", () => {
  const serialized = serializeProjectSessionTabState(emptyProjectSessionState());
  const restored = projectSessionReducer(emptyProjectSessionState("other"), {
    kind: "restore_tab",
    project_id: null,
    state: serialized,
  });

  assert.deepEqual(serializeProjectSessionTabState(restored), serialized);
});

test("a replacement backend accepts its own revision and invalidates old Sync fences", () => {
  const oldHead = { target: { kind: "main" }, revision: 9, transition_id: transitionOne };
  const fence = { project_id: "alpha", request_id: 1, expected_head: oldHead, draft_generation: 0 };
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(9, { graph_head: oldHead }),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, {
    kind: "sync_started",
    fence,
    snapshot_request_id: 1,
    sync_request_sequence: 1,
  });
  assert.equal(transitionSyncCompletionDisposition(state.transitionCoordinator, fence), "apply");
  state = projectSessionReducer(state, {
    kind: "reset",
    project_id: "alpha",
    invalidate_backend: true,
  });
  assert.equal(
    transitionSyncCompletionDisposition(state.transitionCoordinator, fence),
    "reload_active",
  );
  state = projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: snapshot(2),
    preserve_readiness: false,
  });
  assert.equal(state.renderedRevision, 2);
});

test("draft undo and redo restore null, advance generations, and branch after a new edit", () => {
  let state = emptyProjectSessionState("alpha");
  const edit = humanDraft(0);
  state = projectSessionReducer(state, {
    kind: "human_draft_updated",
    project_id: "alpha",
    draft: edit,
  });
  const generation = Object.values(state.transitionCoordinator.draft_generations)[0];
  state = projectSessionReducer(state, { kind: "human_draft_undo", project_id: "alpha" });
  assert.equal(state.humanDraft, null);
  assert.equal(Object.values(state.transitionCoordinator.draft_generations)[0], generation + 1);
  state = projectSessionReducer(state, { kind: "human_draft_redo", project_id: "alpha" });
  assert.deepEqual(state.humanDraft, edit);
  state = projectSessionReducer(state, { kind: "human_draft_undo", project_id: "alpha" });
  state = projectSessionReducer(state, {
    kind: "human_draft_updated",
    project_id: "alpha",
    draft: humanDraft(0, "Replacement"),
  });
  assert.strictEqual(
    projectSessionReducer(state, { kind: "human_draft_redo", project_id: "alpha" }),
    state,
  );
  for (let index = 0; index < 110; index++) {
    state = projectSessionReducer(state, {
      kind: "human_draft_updated",
      project_id: "alpha",
      draft: humanDraft(0, String(index)),
    });
  }
  assert.equal(Object.values(state.draftHistory)[0].past.length, 100);
});

test("a node-detail edit finish folds into its own start step only", () => {
  let state = emptyProjectSessionState("alpha");
  const update = (draft, edit_step) =>
    projectSessionReducer(state, {
      kind: "human_draft_updated",
      project_id: "alpha",
      draft,
      edit_step,
    });
  const undo = () =>
    projectSessionReducer(state, { kind: "human_draft_undo", project_id: "alpha" });
  state = update(humanDraft(0, "Before"));
  state = update(humanDraft(0, "Provisional"), { phase: "start", key: "node" });
  state = update(humanDraft(0, "Finished"), { phase: "finish", key: "node" });
  state = undo();
  assert.deepEqual(state.humanDraft, humanDraft(0, "Before"));
  // A canvas edit in between keeps its own step; the finish no longer folds.
  state = update(humanDraft(0, "Provisional"), { phase: "start", key: "node" });
  state = update(humanDraft(0, "Canvas"));
  state = update(humanDraft(0, "Finished"), { phase: "finish", key: "node" });
  state = undo();
  assert.deepEqual(state.humanDraft, humanDraft(0, "Canvas"));
});

test("draft history stays scoped to its project and graph target across tab restores", () => {
  const canonical = snapshot(1, {
    graph: graph(1, {
      id: "hyp/example",
      type: "hypothesis",
      title: "Original",
      updated_rev: 1,
      standing: "accepted",
    }),
  });
  const poll = { kind: "snapshot_applied", snapshot: canonical, preserve_readiness: false };
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), poll);
  state = projectSessionReducer(state, {
    kind: "human_draft_updated",
    project_id: "alpha",
    draft: humanDraft(1),
  });
  const alpha = serializeProjectSessionTabState(state);
  for (const [projectId, target] of [
    ["beta", { kind: "main" }],
    ["alpha", { kind: "branch", branch_id: "branch/one" }],
  ]) {
    state = projectSessionReducer(state, {
      kind: "reset",
      project_id: projectId,
      graph_target: target,
    });
    assert.strictEqual(
      projectSessionReducer(state, { kind: "human_draft_undo", project_id: projectId }),
      state,
    );
  }
  state = projectSessionReducer(state, { kind: "restore_tab", project_id: "alpha", state: alpha });
  const history = state.draftHistory;
  state = projectSessionReducer(state, poll);
  assert.strictEqual(state.draftHistory, history);
  state = projectSessionReducer(state, { kind: "human_draft_undo", project_id: "alpha" });
  assert.equal(state.humanDraft, null);
});

test("reload, canonical rebase, and Sync clear history while unchanged polls preserve it", () => {
  let state = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });
  state = projectSessionReducer(state, {
    kind: "human_draft_updated",
    project_id: "alpha",
    draft: null,
  });
  const polled = projectSessionReducer(state, {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });
  assert.strictEqual(polled.draftHistory, state.draftHistory);
  for (const action of [
    { kind: "human_draft_loaded", draft: state.humanDraft },
    { kind: "snapshot_applied", snapshot: snapshot(2), preserve_readiness: false },
    {
      kind: "committed_transition_applied",
      project_id: "alpha",
      projection: { ...projection(2), canonical: true },
      submitted_draft: { ...humanDraft(1), nodes: {} },
    },
  ]) {
    const cleared = projectSessionReducer(state, action);
    assert.deepEqual(cleared.draftHistory, {});
    assert.strictEqual(
      projectSessionReducer(cleared, { kind: "human_draft_undo", project_id: "alpha" }),
      cleared,
    );
  }
});
