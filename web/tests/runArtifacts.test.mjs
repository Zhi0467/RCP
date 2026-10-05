import assert from "node:assert/strict";
import test from "node:test";
import { artifactsForOperations, orderRunArtifacts } from "../src/experiments/runArtifactsModel.ts";

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

test("server order keeps the run report ahead of earlier child reports without mutating input", () => {
  const report = entry("own-report", null, 5, { supplier: "episode_ending" });
  const worker = entry("worker", "work", 2, { worker_label: "Worker 1" });
  const child = entry("child-report", null, 1, { supplier: "episode_ending" });
  const input = [report, child, worker];
  const ordered = orderRunArtifacts(input);
  assert.deepEqual(ordered, input);
  assert.notEqual(ordered, input);
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
  assert.deepEqual(artifactsForOperations(entries, ["work", "agent", "work"]), [worker, turn]);
  assert.deepEqual(artifactsForOperations(entries, []), []);
  assert.deepEqual(artifactsForOperations(entries, ["wor"]), []);
});
