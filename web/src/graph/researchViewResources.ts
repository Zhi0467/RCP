import { api } from "../core/api";
import type { ResearchViewPins } from "../core/types";
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
