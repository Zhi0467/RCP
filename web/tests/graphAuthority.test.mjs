import assert from "node:assert/strict";
import test from "node:test";

import {
  projectGraphMutationFailureLabel,
  projectGraphMutationsDisabled,
  taskMayMutateGraph,
} from "../src/graphAuthority.ts";

test("degraded replay blocks graph authority and names the last coherent state", () => {
  const project = {
    graph_mutation: {
      available: false,
      reason:
        "Replay stopped at revision 6 (invalid-edge): The accepted patch no longer validates. This is the last coherent graph.",
    },
  };
  assert.equal(projectGraphMutationsDisabled(project), true);
  assert.equal(
    projectGraphMutationFailureLabel(project),
    "Replay stopped at revision 6 (invalid-edge): The accepted patch no longer validates. This is the last coherent graph.",
  );
  assert.equal(
    projectGraphMutationsDisabled({ graph_mutation: { available: true, reason: null } }),
    false,
  );
});

test("only graph-writing task continuations are blocked by degraded replay", () => {
  assert.equal(taskMayMutateGraph({ kind: "refresh", request: {} }), true);
  assert.equal(taskMayMutateGraph({ kind: "project_chat", request: { mode: "work" } }), false);
  assert.equal(taskMayMutateGraph({ kind: "project_chat", request: { mode: "discuss" } }), false);
  assert.equal(taskMayMutateGraph({ kind: "node_chat", request: { mode: "work" } }), false);
  assert.equal(taskMayMutateGraph({ kind: "paper_coach", request: {} }), false);
});
