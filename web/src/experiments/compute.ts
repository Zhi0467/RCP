import type { ComputeConnectionProbe, ComputeBackendProbe } from "../core/types";

export function computeProbePresentation(
  probe: ComputeConnectionProbe | ComputeBackendProbe | null | undefined,
): {
  label: string;
  tone: "ready" | "error" | "pending";
} {
  if (!probe) return { label: "Not probed", tone: "pending" };
  return { label: probe.status_label, tone: probe.status_tone };
}
