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
  projectHeartbeatSnapshotDisposition,
  projectSessionReducer,
  serializeProjectSessionTabState,
} = await server.ssrLoadModule("/src/hooks/projectSession.ts");

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

test("a populated project session survives tab serialization and restoration", () => {
  let populated = projectSessionReducer(emptyProjectSessionState("alpha"), {
    kind: "snapshot_applied",
    snapshot: snapshot(1),
    preserve_readiness: false,
  });
  populated = projectSessionReducer(populated, {
    kind: "human_draft_loaded",
    draft: {
      version: 1,
      base_revision: 1,
      nodes: {},
      removed_node_ids: [],
      proposals: { "proposal/one": { decision: "rejected" } },
      ontology: null,
      custom_nodes: {},
    },
  });
  populated = projectSessionReducer(populated, {
    kind: "manifest_valid",
    project_id: "alpha",
    manifest: { ruleset_tag: "rcp.lifecycle.v1", triggers: [] },
  });
  populated = projectSessionReducer(populated, {
    kind: "draft_preview_changed",
    projection: projection(2),
    conflict: "last valid preview retained",
    pending: true,
  });

  const serialized = serializeProjectSessionTabState(populated);
  const restored = projectSessionReducer(emptyProjectSessionState(), {
    kind: "restore_tab",
    project_id: "alpha",
    state: serialized,
  });

  assert.deepEqual(serializeProjectSessionTabState(restored), serialized);
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

test("a heartbeat that finishes after its inactive tab reactivates reloads the active project", () => {
  assert.deepEqual(
    projectHeartbeatSnapshotDisposition({
      requestedProjectId: "alpha",
      activeProjectId: "alpha",
      tabOpen: true,
      inactiveState: null,
      snapshotRevision: 3,
      renderedRevision: 2,
    }),
    { kind: "reload_active" },
  );
  assert.deepEqual(
    projectHeartbeatSnapshotDisposition({
      requestedProjectId: "alpha",
      activeProjectId: "alpha",
      tabOpen: true,
      inactiveState: null,
      snapshotRevision: 2,
      renderedRevision: 2,
    }),
    { kind: "ignore" },
  );
  assert.deepEqual(
    projectHeartbeatSnapshotDisposition({
      requestedProjectId: "alpha",
      activeProjectId: "alpha",
      tabOpen: false,
      inactiveState: null,
      snapshotRevision: 3,
      renderedRevision: 2,
    }),
    { kind: "ignore" },
  );
});
