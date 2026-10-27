import type { ProviderReadiness } from "../core/types";

/** One provider executable's state on a machine card and its project tile. */
export function providerPathPresentation(
  readiness: ProviderReadiness | undefined,
  value: string,
  recorded: string,
): { label: string; kind: "ready" | "warning" | "error" | "pending" } {
  if (value !== recorded) return { label: "Unsaved", kind: "pending" };
  if (readiness?.path_state === "unreachable")
    return { label: "Machine unreachable", kind: "error" };
  if (readiness?.path_state === "denied") return { label: "Recorded path unusable", kind: "error" };
  if (readiness?.path_state === "missing") {
    return { label: value ? "Recorded path missing" : "Executable missing", kind: "error" };
  }
  if (readiness?.path_state === "resolved") return { label: "Ready", kind: "ready" };
  return { label: "Not recorded", kind: "warning" };
}
