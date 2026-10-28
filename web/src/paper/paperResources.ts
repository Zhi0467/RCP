import { api } from "../core/api";
import type { ResourceKey, ResourceProfile } from "../core/resourceStore";
import type { PaperSnapshot, PaperTree, WritingSession } from "../core/types";

/** Each link opening is a new request, including a return to the same file. */
export function paperFileRequest(hash: string) {
  return { fileId: new URLSearchParams(hash.split("?")[1]).get("paper_file") };
}

export interface PaperResource {
  tree: PaperTree;
  /** Shell badge projection until the shell consumes PaperTree directly. */
  paper: PaperSnapshot;
  sessions: WritingSession[];
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
      const [tree, sessions] = await Promise.all([
        api<PaperTree>(`${apiBase}/paper/tree`, { signal }),
        api<WritingSession[]>(`${apiBase}/paper/sessions`, { signal }),
      ]);
      return {
        tree,
        sessions,
        paper: {
          content: "",
          draft_version: null,
          sync_state: tree.publish_state === "published" ? "synced" : "unsynced",
          canonical_available: tree.files.length > 0,
        },
      };
    },
  };
}
