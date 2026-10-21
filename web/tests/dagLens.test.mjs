import assert from "node:assert/strict";
import test from "node:test";

import { EMPTY_DAG_LENS, lensBrightEdge, lensBrightNode, lensShows } from "../src/graph/dagLens.ts";

const lit = { id: "h/lit", type: "hypothesis" };
const neighbor = { id: "e/near", type: "evidence" };

test("a lit node and its own edges brighten while its neighbors stay dim", () => {
  const lens = { ...EMPTY_DAG_LENS, litNodeIds: new Set([lit.id]) };
  assert.equal(lensBrightNode(lens, lit), true);
  assert.equal(lensBrightEdge(lens, lit, neighbor), true);
  assert.equal(lensBrightNode(lens, neighbor), false);
  assert.equal(lensBrightEdge(EMPTY_DAG_LENS, lit, neighbor), false);
});

test("a hidden type is not shown, and neither is a missing node", () => {
  const lens = { ...EMPTY_DAG_LENS, hiddenTypes: new Set(["evidence"]) };
  assert.equal(lensShows(lens, neighbor), false);
  assert.equal(lensShows(lens, lit), true);
  assert.equal(lensShows(lens, undefined), false);
});
