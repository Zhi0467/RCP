import assert from "node:assert/strict";
import test from "node:test";

import {
  projectGraphMutationFailureLabel,
  projectGraphMutationsDisabled,
} from "../src/graph/graphAuthority.ts";

test("degraded replay blocks graph authority and names the last coherent state", () => {
  const project = {
    graph_mutation: {
      available: false,
      reason:
        "Replay stopped at revision 6 (invalid-edge): The accepted patch no longer validates. This is the last coherent graph.",
    },
  };
  assert.equal(projectGraphMutationsDisabled(project), true);
  assert.equal(projectGraphMutationFailureLabel(project), project.graph_mutation.reason);

  assert.equal(
    projectGraphMutationsDisabled({ graph_mutation: { available: true, reason: null } }),
    false,
  );
});
