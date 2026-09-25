import assert from "node:assert/strict";
import test from "node:test";

import { presentNode } from "../src/nodePresentation.ts";

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

  assert.equal(presentation.value, "SDFT improves retention.");
});

test("Evidence presentation separates methodological role from labelled legacy strength", () => {
  const presentation = presentNode({
    id: "ev/example",
    type: "evidence",
    title: "Example result",
    observation: "The held-out score improved.",
    interpretation: "The change matters in the tested regime.",
    role: "result",
    legacy_strength: "supporting",
  });

  assert.deepEqual(
    presentation.context.map(({ value }) => value),
    ["The change matters in the tested regime.", "result", "supporting"],
  );
});
