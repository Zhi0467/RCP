import assert from "node:assert/strict";
import { test } from "node:test";
import {
  catchUpProjectDigest,
  digestChangedNodeIds,
  projectDigestIsEmpty,
} from "../src/projects/projectDigest.ts";

const digest = (overrides = {}) => ({
  cursor: 0,
  mark: null,
  needs_you: [],
  changed: [],
  reports: [],
  changed_node_ids: [],
  count: 0,
  ...overrides,
});

const reportItem = (seq) => ({
  kind: "episode_report",
  item_id: `report_${seq}`,
  title: `report ${seq}`,
  report_artifact_id: null,
  deep_link: null,
  created_at: "2026-10-03T00:00:00Z",
});

test("the digest is empty only when all three groups are empty", () => {
  assert.equal(projectDigestIsEmpty(null), true);
  assert.equal(projectDigestIsEmpty(digest({ cursor: 9 })), true);
  for (const group of ["needs_you", "changed", "reports"]) {
    assert.equal(projectDigestIsEmpty(digest({ [group]: [{}] })), false, group);
  }
});

test("Caught up marks the on-screen cursor and keeps items recorded after it", async () => {
  const server = { events: [1, 2], mark: 0, marks: [] };
  const read = async () => {
    const seqs = server.events.filter((seq) => seq > server.mark);
    return digest({ cursor: Math.max(0, ...server.events), reports: seqs.map(reportItem) });
  };
  let onScreen = await read();
  assert.deepEqual(
    onScreen.reports.map((item) => item.item_id),
    ["report_1", "report_2"],
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
    onScreen.reports.map((item) => item.item_id),
    ["report_3"],
  );
});

test("dots apply to the main target only", () => {
  const changed = digest({ changed_node_ids: ["n1", "n2"] });
  assert.deepEqual([...digestChangedNodeIds(changed, { kind: "main" })].sort(), ["n1", "n2"]);
  assert.equal(digestChangedNodeIds(changed, { kind: "branch", branch_id: "ep_2" }).size, 0);
  assert.equal(digestChangedNodeIds(null, { kind: "main" }).size, 0);
});
