import assert from "node:assert/strict";
import test from "node:test";

import {
  computeProbePresentation,
  latestPersistedComputeIds,
  reconcileActiveComputeIds,
} from "../src/compute.ts";
import { appStylesheet, withResolvedTypeScale } from "./appStylesheet.mjs";

const connections = [
  { id: "local", name: "Local", kind: "local", ssh_target: "", access_hint: "" },
  { id: "gpu", name: "GPU", kind: "ssh", ssh_target: "alice@gpu", access_hint: "" },
];

test("compute selection removes deleted connections and duplicate ids", () => {
  assert.deepEqual(reconcileActiveComputeIds(["gpu", "missing", "gpu"], connections), ["gpu"]);
});

test("the newest persisted chat or task selection restores composer state", () => {
  const messages = [
    {
      timestamp: "2026-09-02T10:00:00Z",
      active_compute_ids: ["local"],
    },
  ];
  const tasks = [
    {
      created_at: "2026-09-02T10:01:00Z",
      request: { active_compute_ids: ["gpu"] },
    },
  ];
  assert.deepEqual(latestPersistedComputeIds(messages, tasks, connections), ["gpu"]);
});

test("compute probes expose distinct failure labels", () => {
  assert.deepEqual(
    computeProbePresentation({
      state: "sealed-backend-state",
      status_label: "Authentication failed",
      status_tone: "error",
    }),
    { label: "Authentication failed", tone: "error" },
  );
  assert.deepEqual(
    computeProbePresentation({
      state: "contradictory-raw-value",
      status_label: "Reachable",
      status_tone: "ready",
    }),
    {
      label: "Reachable",
      tone: "ready",
    },
  );
});

test("compute controls introduce no sub-10px primary or status text", () => {
  const styles = withResolvedTypeScale(appStylesheet());
  const composer = styles.slice(
    styles.indexOf(".chat-compute-picker"),
    styles.indexOf(".artifact-context-chip"),
  );
  const settings = styles.slice(
    styles.indexOf(".compute-settings > header"),
    styles.indexOf(".agent-machine-fixed"),
  );

  assert.doesNotMatch(composer, /font(?:-size)?\s*:[^;\n]*\b[0-9]px/);
  assert.doesNotMatch(settings, /font(?:-size)?\s*:[^;\n]*\b[0-9]px/);
});

test("backend probes use the same server-owned presentation, including pending", () => {
  assert.deepEqual(computeProbePresentation(null).tone, "pending");
  assert.deepEqual(
    computeProbePresentation({
      backend_id: "slurm",
      ready: false,
      state: "opaque",
      status_label: "Available",
      status_tone: "ready",
    }),
    { label: "Available", tone: "ready" },
  );
});
