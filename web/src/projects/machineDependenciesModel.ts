import type { DependencyStatus, MissingProgram } from "../core/types";

const platformNames: Record<string, string> = { darwin: "macOS", linux: "Linux" };

/** What the machine card's Dependencies row shows for one check. */
export interface DependencyView {
  readonly outcome: DependencyStatus["outcome"];
  /** The checked OS and distribution, when the check got that far. */
  readonly system: string | null;
  /** A Linux distribution RCP has not been tested on; allowed, never refused. */
  readonly untested: boolean;
  /** Missing programs that refuse agent runs on this machine. */
  readonly required: readonly MissingProgram[];
  /** Missing programs that only turn off one feature. */
  readonly optional: readonly MissingProgram[];
  /** Why the machine is unsupported or was not checked. */
  readonly reason: string | null;
  /** A copyable install line; otherwise `notes` lists the steps. */
  readonly command: string | null;
  readonly notes: readonly string[];
}

export function dependencyView(status: DependencyStatus): DependencyView {
  const platform = status.platform ? (platformNames[status.platform] ?? status.platform) : null;
  const system = [platform, status.distribution].filter(Boolean).join(" · ") || null;
  const anyMissing = status.missing.length > 0;
  return {
    outcome: status.outcome,
    system,
    untested: !status.tested,
    required: status.missing.filter((program) => program.required),
    optional: status.missing.filter((program) => !program.required),
    reason:
      status.outcome === "unsupported" || status.outcome === "not_checked" ? status.reason : null,
    command: anyMissing ? status.install_command : null,
    notes: anyMissing && !status.install_command ? status.install_notes : [],
  };
}

export function dependencyLabel(view: DependencyView): string {
  switch (view.outcome) {
    case "missing":
      return "Required programs missing";
    case "unsupported":
      return "Unsupported";
    case "not_checked":
      return "Not checked";
    case "ready":
      return view.optional.length ? "Ready, some features off" : "Ready";
  }
}
