import { api } from "../core/api";
import type { ResourceKey, ResourceProfile } from "../core/resourceStore";
import type { PaperTree } from "../core/types";

/** Each link opening is a new request, including a return to the same file. */
export function paperFileRequest(hash: string) {
  return { fileId: new URLSearchParams(hash.split("?")[1]).get("paper_file") };
}

export interface PaperResource {
  tree: PaperTree;
  /** Whether saved changes still need publication. */
  paper: boolean;
}
export function paperResourceKey(projectId: string): ResourceKey {
  return { backend: "", member: "", project: projectId, name: "paper" };
}
export function paperProfile(
  projectId: string,
  apiBase = `/api/projects/${encodeURIComponent(projectId)}`,
): ResourceProfile<PaperResource> {
  return {
    key: paperResourceKey(projectId),
    async fetch(signal) {
      const tree = await api<PaperTree>(`${apiBase}/paper/tree`, { signal });
      return {
        tree,
        paper: tree.publish_state !== "published",
      };
    },
  };
}
