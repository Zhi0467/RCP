import type { GraphRef, GraphTargetRef } from "./types";

/** Lowercase letters, digits, and hyphens; 2–32 characters; never `main`. */
export const BRANCH_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$/;

/** What a branch-name field keeps as the user types: lowercase, each run of spaces one hyphen. */
export function typedBranchName(value: string): string {
  return value.toLowerCase().replace(/\s+/g, "-");
}

export function branchNameProblem(name: string): string | null {
  if (name === "") return null;
  if (name === "main") return "main is reserved.";
  return BRANCH_NAME_PATTERN.test(name) ? null : "Use 2–32 lowercase letters, digits, and hyphens.";
}

/** The one display name for a graph target; every surface reads it from here. */
export function branchName(
  refs: readonly GraphRef[] | null | undefined,
  target: GraphTargetRef | null | undefined,
): string {
  if (!target || target.kind === "main") return "main";
  const ref = refs?.find((item) => item.kind === "branch" && item.branch_id === target.branch_id);
  return ref?.name ?? target.branch_id.slice(0, 8);
}
