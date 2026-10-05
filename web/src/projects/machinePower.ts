import type { MachinePowerStatus } from "../core/types";

export function showMachinePowerCard(
  spaceKind: "personal" | "team" | undefined,
  status: MachinePowerStatus | null,
): boolean {
  return spaceKind === "personal" && status?.supported === true;
}

export function machinePowerWarnings(status: MachinePowerStatus | null) {
  return {
    latch: status?.latched ?? null,
    cleanup: status?.cleanup_failure ?? null,
  };
}
