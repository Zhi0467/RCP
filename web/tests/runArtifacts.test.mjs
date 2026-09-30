import assert from "node:assert/strict";
import test from "node:test";
import { artifactsForOperations, orderRunArtifacts } from "../src/runArtifacts.ts";

const entry = (id, operation, minute, extra = {}) => ({
  artifact_id: id,
  name: id,
  supplier: "turn",
  origin_operation_id: operation,
  worker_label: null,
  view: "html",
  media_type: "text/html",
  created_at: new Date(Date.UTC(2026, 0, 1, 0, minute)).toISOString(),
  ...extra,
});

test("reports lead all turn and worker artifacts without mutating input", () => {
  const worker = entry("worker", "work", 2, { worker_label: "Worker 1" });
  const report = entry("report", null, 3, { supplier: "episode_ending" });
  const turn = entry("turn", "agent", 1);
  const input = [worker, report, turn];
  assert.deepEqual(orderRunArtifacts(input), [report, turn, worker]);
  assert.deepEqual(input, [worker, report, turn]);
});

test("turn popovers match exact producing operations and exclude reports and unknown origins", () => {
  const worker = entry("worker", "work", 2, { worker_label: "Worker 1" });
  const turn = entry("turn", "agent", 1);
  const entries = [
    worker,
    turn,
    entry("unknown", null, 3),
    entry("report", "work", 4, { supplier: "episode_ending" }),
  ];
  assert.deepEqual(artifactsForOperations(entries, ["work"]), [worker]);
  assert.deepEqual(artifactsForOperations(entries, ["work", "agent", "work"]), [turn, worker]);
  assert.deepEqual(artifactsForOperations(entries, []), []);
  assert.deepEqual(artifactsForOperations(entries, ["wor"]), []);
});
