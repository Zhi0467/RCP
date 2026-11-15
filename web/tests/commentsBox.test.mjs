import test from "node:test";
import assert from "node:assert/strict";
import { boxFractions } from "../src/comments/box.ts";

test("box fractions follow reverse drag and clamp to surface", () => {
  assert.deepEqual(
    boxFractions({ x: 250, y: 180 }, { x: 0, y: 80 }, { x: 50, y: 100, width: 200, height: 100 }),
    { x: 0, y: 0, width: 1, height: 0.8 },
  );
});

test("empty and outside boxes are not selections", () => {
  assert.equal(
    boxFractions({ x: 0, y: 0 }, { x: 10, y: 10 }, { x: 50, y: 50, width: 100, height: 100 }),
    null,
  );
  assert.equal(
    boxFractions({ x: 50, y: 50 }, { x: 50, y: 90 }, { x: 50, y: 50, width: 100, height: 100 }),
    null,
  );
});
