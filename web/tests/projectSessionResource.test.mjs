import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { useProjectSession } = await server.ssrLoadModule("/src/graph/useProjectSession.ts");
const { RefreshRuntimeContext } = await server.ssrLoadModule("/src/core/useResource.ts");
const { ResourceStore } = await server.ssrLoadModule("/src/core/resourceStore.ts");
const { graphResourceKey } = await server.ssrLoadModule("/src/graph/graphResources.ts");
const { emptyProjectSessionState } = await server.ssrLoadModule("/src/graph/projectSession.ts");
after(() => server.close());

test("cached graph selection is stable and local preview changes survive it", () => {
  const store = new ResourceStore();
  const graph = {
    revision: 1,
    nodes: {},
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
  const snapshot = {
    id: "p",
    revision: 1,
    name: "P",
    snapshot_freshness: "fresh",
    graph,
    attention: {
      pending_proposal_ids: [],
      decisions_awaiting_choice_ids: [],
      open_blocker_ids: [],
      proposal_actions: {},
      decision_prior_choices: {},
    },
    experiment_control: {},
  };
  const id = store.register("tab", {
    key: graphResourceKey("p"),
    fetch: () => new Promise(() => {}),
  });
  store.write(id, snapshot);
  let session;
  function Harness() {
    session = useProjectSession(null);
    return null;
  }
  renderToStaticMarkup(
    React.createElement(
      RefreshRuntimeContext.Provider,
      { value: { store } },
      React.createElement(Harness),
    ),
  );
  session.restoreProjectSessionTab("p", emptyProjectSessionState("p"));
  const selected = session.getProjectSessionState();
  assert.equal(selected.project.id, "p");
  assert.strictEqual(session.getProjectSessionState(), selected);
  session.dispatchProjectSession({
    kind: "draft_preview_changed",
    projection: null,
    conflict: "keep",
    pending: true,
  });
  assert.equal(session.getProjectSessionState().draftPreviewConflict, "keep");
  assert.equal(session.getProjectSessionState().draftPreviewPending, true);
  store.write(id, { ...snapshot, experiment_control: { changed: {} } });
  assert.deepEqual(session.getProjectSessionState().project.experiment_control, { changed: {} });
  assert.equal(session.getProjectSessionState().draftPreviewPending, false);
  store.release("tab");
});
