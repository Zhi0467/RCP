import assert from "node:assert/strict";
import { test } from "node:test";
import {
  catchUpProjectDigest,
  createDigestRequestFence,
  digestChangedNodeIds,
  projectDigestIsEmpty,
} from "../src/projects/projectDigest.ts";

const digest = (overrides = {}) => ({
  cursor: 0,
  mark: null,
  needs_you: [],
  changed: [],
  branches: [],
  ran: [],
  changed_node_ids: [],
  count: 0,
  ...overrides,
});

const ranItem = (seq) => ({
  kind: "job_ended",
  item_id: `job_${seq}`,
  title: `job ${seq}`,
  status: "exited",
  deep_link: null,
  created_at: "2026-10-03T00:00:00Z",
});

test("the digest is empty only when all four groups are empty", () => {
  assert.equal(projectDigestIsEmpty(null), true);
  assert.equal(projectDigestIsEmpty(digest({ cursor: 9 })), true);
  for (const group of ["needs_you", "changed", "branches", "ran"]) {
    assert.equal(projectDigestIsEmpty(digest({ [group]: [{}] })), false, group);
  }
});

test("a late response for an older request is dropped in either arrival order", () => {
  const inOrder = createDigestRequestFence();
  const first = inOrder.begin("p");
  const second = inOrder.begin("p");
  assert.equal(inOrder.accept("p", first), true);
  assert.equal(inOrder.accept("p", second), true);

  const reversed = createDigestRequestFence();
  const older = reversed.begin("p");
  const newer = reversed.begin("p");
  assert.equal(reversed.accept("p", newer), true);
  assert.equal(reversed.accept("p", older), false);
});

test("a response for a project that is no longer open is dropped", () => {
  const fence = createDigestRequestFence();
  const forA = fence.begin("a");
  const forB = fence.begin("b");
  assert.equal(fence.accept("a", forA), false);
  assert.equal(fence.accept("b", forB), true);
});

test("invalidate drops reads issued before Caught up", () => {
  const fence = createDigestRequestFence();
  const before = fence.begin("p");
  fence.invalidate();
  const after = fence.begin("p");
  assert.equal(fence.accept("p", before), false);
  assert.equal(fence.accept("p", after), true);
});

test("Caught up marks the on-screen cursor and keeps items recorded after it", async () => {
  const server = { events: [1, 2], mark: 0, marks: [] };
  const read = async () => {
    const seqs = server.events.filter((seq) => seq > server.mark);
    return digest({ cursor: Math.max(0, ...server.events), ran: seqs.map(ranItem) });
  };
  let onScreen = await read();
  assert.deepEqual(
    onScreen.ran.map((item) => item.item_id),
    ["job_1", "job_2"],
  );
  server.events.push(3); // arrives while the reader looks at the card

  await catchUpProjectDigest("p", onScreen, {
    mark: async (projectId, seq) => {
      server.marks.push([projectId, seq]);
      server.mark = Math.max(server.mark, seq);
    },
    reload: async () => {
      onScreen = await read();
    },
  });

  assert.deepEqual(server.marks, [["p", 2]]);
  assert.deepEqual(
    onScreen.ran.map((item) => item.item_id),
    ["job_3"],
  );
});

test("dots apply to the main target only", () => {
  const changed = digest({ changed_node_ids: ["n1", "n2"] });
  assert.deepEqual([...digestChangedNodeIds(changed, { kind: "main" })].sort(), ["n1", "n2"]);
  assert.equal(digestChangedNodeIds(changed, { kind: "branch", branch_id: "ep_2" }).size, 0);
  assert.equal(digestChangedNodeIds(null, { kind: "main" }).size, 0);
});
