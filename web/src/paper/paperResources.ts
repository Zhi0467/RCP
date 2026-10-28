import { api } from "../core/api";
import type { ResourceProfile } from "../core/resourceStore";
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
export function paperProfile(projectId: string): ResourceProfile<PaperResource> {
  return {
    key: { backend: "", member: "", project: projectId, name: "paper" },
    async fetch(signal) {
      const tree = await api<PaperTree>(
        `/api/projects/${encodeURIComponent(projectId)}/paper/tree`,
        { signal },
      );
      return {
        tree,
        paper: tree.publish_state !== "published",
      };
    },
  };
}
