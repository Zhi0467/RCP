import assert from "node:assert/strict";
import test from "node:test";

import { nodeTypeLabel, presentNode } from "../src/nodePresentation.ts";

test("node presentation promotes the claim and human-readable context", () => {
  const node = {
    id: "hyp/example",
    type: "hypothesis",
    title: "Example",
    statement: "SDFT improves retention.",
    rationale: "The update reuses prior trajectories.",
    predictions: ["Less forgetting after the next update"],
  };
  const presentation = presentNode(node);
  assert.equal(presentation.label, "Claim");
  assert.equal(presentation.value, "SDFT improves retention.");
  assert.deepEqual(
    presentation.context.map(({ label }) => label),
    ["Reasoning", "What should happen if this is right"],
  );
});

test("custom nodes keep their extension label even after the definition is removed", () => {
  assert.equal(
    nodeTypeLabel({ type: "hypothesis", extension_type: "mechanism_hypothesis" }),
    "Mechanism hypothesis",
  );
  assert.equal(nodeTypeLabel({ type: "hypothesis" }), "Hypothesis");
});
