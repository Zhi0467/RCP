import { api } from "../core/api";
import type { GraphTargetRef, ResearchViewPins } from "../core/types";
import type { ResourceProfile } from "../core/resourceStore";
import { projectResourceKey } from "../projects/projectResources";

export function researchViewsUrl(projectId: string, branchId: string | null): string {
  return `/api/projects/${encodeURIComponent(projectId)}/research-views?branch_id=${encodeURIComponent(branchId ?? "")}`;
}

export function researchViewsProfile(
  projectId: string,
  branchId: string | null,
): ResourceProfile<ResearchViewPins> {
  return {
    key: projectResourceKey(projectId, "artifacts", {
      query: `research-views:${JSON.stringify(branchId)}`,
    }),
    fetch: (signal) => api(researchViewsUrl(projectId, branchId), { signal }),
  };
}

/** One project and graph target's pin row; selections and route requests share it. */
export function pinScopeKey(projectId: string | null, branchId: string | null): string {
  return JSON.stringify([projectId, branchId]);
}

/** The pin a route asks to open, scoped to the target whose pin row holds it. */
export function pinRequest(
  projectId: string | null,
  target: GraphTargetRef,
  pinId: string | undefined,
): { scope: string; id: string } | null {
  if (!projectId || !pinId) return null;
  return {
    scope: pinScopeKey(projectId, target.kind === "branch" ? target.branch_id : null),
    id: pinId,
  };
}
