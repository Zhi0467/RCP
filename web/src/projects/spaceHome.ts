/** Explicit links win; a personal home restores the member's last tab. */
export function spaceHomeHash(
  hash: string,
  spaceKind: string | undefined,
  remembered: string | null,
): string {
  if (spaceKind !== "personal" || !["", "#", "#/"].includes(hash)) return hash;
  return remembered === "cards" ? hash : "#/agent";
}
