import assert from "node:assert/strict";
import test from "node:test";

import { buildRunTaskProjection, latestRunObservation } from "../src/runProjection.ts";

function task(operationId, status, createdAt, parentOperationId = null) {
  return {
    operation_id: operationId,
    project_id: "project",
    kind: "refresh",
    status,
    request: {},
    created_at: createdAt,
    updated_at: createdAt,
    status_message: status,
    attempt: 1,
    parent_operation_id: parentOperationId,
  };
}

test("task retries group under their logical root and classify by the latest attempt", () => {
  const projection = buildRunTaskProjection([
    task("root", "failed", "2026-07-28T00:00:00Z"),
    task("retry-1", "failed", "2026-07-28T01:00:00Z", "root"),
    task("retry-2", "running", "2026-07-28T02:00:00Z", "retry-1"),
    task("paused", "paused", "2026-07-28T03:00:00Z"),
    task("done", "succeeded", "2026-07-28T04:00:00Z"),
  ]);

  assert.deepEqual(
    projection.running[0].attempts.map((item) => item.operation_id),
    ["root", "retry-1", "retry-2"],
  );
  assert.equal(projection.running[0].latest.operation_id, "retry-2");
  assert.deepEqual(
    projection.actionable.map((group) => group.rootId),
    ["paused"],
  );
  assert.deepEqual(
    projection.completed.map((group) => group.rootId),
    ["done"],
  );
});

test("runs report the newest underlying graph or task observation", () => {
  const newerTask = task("task", "running", "2026-07-28T03:00:00Z");

  assert.equal(latestRunObservation("2026-07-28T02:00:00Z", [newerTask]), newerTask.updated_at);
  assert.equal(latestRunObservation("2026-07-28T04:00:00Z", [newerTask]), "2026-07-28T04:00:00Z");
  assert.equal(latestRunObservation(null, []), null);
});

test("dismissed and superseded failures leave the action queue", () => {
  const failed = task("failed", "failed", "2026-07-28T00:00:00Z");
  const laterSuccess = task("later", "succeeded", "2026-07-28T01:00:00Z");
  const dismissed = task("dismissed", "failed", "2026-07-28T02:00:00Z");
  const projection = buildRunTaskProjection(
    [laterSuccess, dismissed, failed],
    new Set(["dismissed"]),
  );
  assert.deepEqual(projection.actionable, []);
  assert.deepEqual(
    projection.completed.map((group) => group.rootId),
    ["later"],
  );
});
