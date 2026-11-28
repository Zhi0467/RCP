import type { ProviderReadiness } from "../core/types";

/** One provider executable's state on a machine card and its project tile. */
export function providerPathPresentation(
  readiness: ProviderReadiness | undefined,
  value: string,
  recorded: string,
  update?: { updating: boolean; failure: string | null },
): { label: string; kind: "ready" | "warning" | "error" | "pending"; note?: string } {
  if (update?.updating || (readiness?.updating && value === recorded))
    return { label: "Updating", kind: "pending" };
  if (update?.failure) return { label: "Update failed", kind: "warning", note: update.failure };
  if (value !== recorded) return { label: "Unsaved", kind: "pending" };
  if (readiness?.path_state === "unreachable")
    return { label: "Machine unreachable", kind: "error" };
  if (readiness?.path_state === "denied") return { label: "Recorded path unusable", kind: "error" };
  if (readiness?.path_state === "missing") {
    return { label: value ? "Recorded path missing" : "Executable missing", kind: "error" };
  }
  if (readiness?.path_state === "resolved") {
    if (readiness.update_warning)
      return { label: "Update failed", kind: "warning", note: readiness.update_warning };
    const version = readiness.version ? ` · ${readiness.version}` : "";
    if (readiness.updated_from == null) return { label: `Ready${version}`, kind: "ready" };
    return readiness.updated_from === readiness.version
      ? { label: `Up to date${version}`, kind: "ready" }
      : { label: `Updated${version}`, kind: "ready", note: `From ${readiness.updated_from}` };
  }
  return { label: "Not recorded", kind: "warning" };
}
