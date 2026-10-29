import assert from "node:assert/strict";
import test from "node:test";

import { computeProbePresentation } from "../src/experiments/compute.ts";
import { appStylesheet, withResolvedTypeScale } from "./appStylesheet.mjs";

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
    styles.indexOf(".chat-composer.is-dragging-files"),
  );
  const settings = styles.slice(
    styles.indexOf(".machine-compute {"),
    styles.indexOf(".agent-runtime-fixed"),
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
