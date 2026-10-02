import type { MachinePowerStatus } from "./types";

export function showMachinePowerCard(
  spaceKind: "personal" | "team" | undefined,
  status: MachinePowerStatus | null,
): boolean {
  return spaceKind === "personal" && status?.supported === true;
}
