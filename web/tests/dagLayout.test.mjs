import assert from "node:assert/strict";
import test from "node:test";

import {
  buildSemanticLaneLayout,
  buildTopologyLayout,
  rectangleCollisionCandidates,
  resolveRectangleCollisions,
} from "../src/hooks/dagLayout.ts";

test("semantic stages stay left-to-right across reverse-reading relations", () => {
  const nodes = [
    { id: "evidence", type: "evidence" },
    { id: "blocker", type: "blocker" },
    { id: "experiment", type: "experiment" },
    { id: "decision", type: "decision" },
    { id: "hypothesis", type: "hypothesis" },
    { id: "question", type: "research_question" },
  ];
  const edges = [
    { source: "experiment", target: "hypothesis", relation: "tests" },
    { source: "experiment", target: "decision", relation: "governed_by" },
    { source: "evidence", target: "experiment", relation: "result_of" },
  ];

  const layout = buildSemanticLaneLayout(nodes, edges);
  const reordered = buildSemanticLaneLayout([...nodes].reverse(), [...edges].reverse());
  const laneById = Object.fromEntries(
    layout.lanes.flatMap((lane, index) => lane.map((id) => [id, index])),
  );

  assert.deepEqual(layout, reordered);
  assert.equal(laneById.question, 0);
  assert.equal(laneById.hypothesis, 1);
  assert.equal(laneById.decision, 1);
  assert.equal(laneById.experiment, 2);
  assert.equal(laneById.blocker, 2);
  assert.equal(laneById.evidence, 3);
  assert.ok(laneById.experiment > laneById.hypothesis);
  assert.ok(laneById.experiment > laneById.decision);
  assert.ok(laneById.evidence > laneById.experiment);
});

test("topology ranks condense cycles and remain deterministic", () => {
  const nodes = ["z", "d", "b", "c", "a"].map((id) => ({ id }));
  const edges = [
    { source: "c", target: "d" },
    { source: "a", target: "b" },
    { source: "b", target: "c" },
    { source: "b", target: "a" },
  ];

  const layout = buildTopologyLayout(nodes, edges);
  const reordered = buildTopologyLayout([...nodes].reverse(), [...edges].reverse());

  assert.deepEqual(layout, reordered);
  assert.equal(layout.rankById.a, 0);
  assert.equal(layout.rankById.b, 0);
  assert.equal(layout.rankById.c, 1);
  assert.equal(layout.rankById.d, 2);
  assert.equal(layout.rankById.z, 0);
});

test("parent and child barycenters remove a simple avoidable crossing", () => {
  const nodes = ["a", "b", "c", "d"].map((id) => ({ id }));
  const edges = [
    { source: "a", target: "d" },
    { source: "b", target: "c" },
  ];

  const layout = buildTopologyLayout(nodes, edges);

  assert.deepEqual(layout.layers, [
    ["a", "b"],
    ["d", "c"],
  ]);
  assert.equal(countCrossings(layout.layers, edges), 0);
  assert.equal(
    countCrossings(
      [
        ["a", "b"],
        ["c", "d"],
      ],
      edges,
    ),
    1,
  );
});

test("spatial hash retains every overlap while pruning a 400-node candidate set", () => {
  const nodes = Array.from({ length: 400 }, (_, index) => ({
    x: (index % 20) * 300,
    y: Math.floor(index / 20) * 160,
  }));
  nodes[1] = { x: 100, y: 30 };

  const candidates = rectangleCollisionCandidates(nodes, 260, 120);
  const candidateKeys = new Set(candidates.map(([left, right]) => `${left}:${right}`));
  const overlaps = exhaustiveOverlaps(nodes, 260, 120);

  overlaps.forEach(([left, right]) => assert.ok(candidateKeys.has(`${left}:${right}`)));
  assert.ok(overlaps.length > 0);
  assert.ok(candidates.length < (nodes.length * (nodes.length - 1)) / 40);
});

test("rectangle collision moves only the unpinned node", () => {
  const pinned = { x: 0, y: 0, vx: 0, vy: 0, fx: 0, fy: 0 };
  const free = { x: 100, y: 20, vx: 0, vy: 0, fx: null, fy: null };

  resolveRectangleCollisions([pinned, free], 260, 120, 0.94, 1);

  assert.equal(pinned.vx, 0);
  assert.equal(pinned.vy, 0);
  assert.notEqual(free.vy, 0);
});

function exhaustiveOverlaps(nodes, collisionWidth, collisionHeight) {
  const overlaps = [];
  for (let left = 0; left < nodes.length; left += 1) {
    for (let right = left + 1; right < nodes.length; right += 1) {
      const dx = Math.abs(nodes[right].x - nodes[left].x);
      const dy = Math.abs(nodes[right].y - nodes[left].y);
      if (dx < collisionWidth && dy < collisionHeight) overlaps.push([left, right]);
    }
  }
  return overlaps;
}

function countCrossings(layers, edges) {
  const positions = new Map();
  layers.forEach((layer, rank) => {
    layer.forEach((id, order) => positions.set(id, { rank, order }));
  });
  let crossings = 0;
  for (let left = 0; left < edges.length; left += 1) {
    for (let right = left + 1; right < edges.length; right += 1) {
      const leftSource = positions.get(edges[left].source);
      const leftTarget = positions.get(edges[left].target);
      const rightSource = positions.get(edges[right].source);
      const rightTarget = positions.get(edges[right].target);
      if (
        leftSource?.rank === rightSource?.rank &&
        leftTarget?.rank === rightTarget?.rank &&
        Math.sign(leftSource.order - rightSource.order) !==
          Math.sign(leftTarget.order - rightTarget.order)
      )
        crossings += 1;
    }
  }
  return crossings;
}
