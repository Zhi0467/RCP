import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  computeProbePresentation,
  latestPersistedComputeIds,
  reconcileActiveComputeIds,
} from "../src/compute.ts";

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
  assert.equal(computeProbePresentation(undefined).label, "Not probed");
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

test("the composer compute checklist uses native checkbox semantics", () => {
  const source = readFileSync(new URL("../src/components/NodeChat.tsx", import.meta.url), "utf8");
  const checklist = source.slice(
    source.indexOf("{computeMenuOpen ? ("),
    source.indexOf(") : null}", source.indexOf("{computeMenuOpen ? (")),
  );

  assert.match(checklist, /<fieldset[^>]*aria-label="Compute connections"/);
  assert.match(checklist, /type="checkbox"/);
  assert.doesNotMatch(checklist, /role="menu"|role="menuitemcheckbox"/);
});

test("compute controls introduce no sub-10px primary or status text", () => {
  const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");
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
