import { api, ApiError } from "../core/api";
import {
  resourceKeyId,
  type ResourceKey,
  type ResourceProfile,
  type ResourceStore,
} from "../core/resourceStore";
import type { PaperSnapshot, WritingSession } from "../core/types";

export interface PaperResource {
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
      const [paper, sessions] = await Promise.all([
        api<PaperSnapshot>(`${apiBase}/paper`, { signal }),
        api<WritingSession[]>(`${apiBase}/paper/sessions`, { signal }),
      ]);
      return { paper, sessions };
    },
  };
}

/** A refused save reloads metadata; only the editor owns its typed buffer. */
export async function savePaperDraft(
  store: ResourceStore,
  profile: ResourceProfile<PaperResource>,
  content: string,
  baseHash: string | null,
  draftRevision: number,
): Promise<PaperSnapshot> {
  const id = resourceKeyId(profile.key);
  const mutation = store.beginMutation(
    `paper-save:${crypto.randomUUID()}`,
    new Map([[id, (value: unknown) => value]]),
  );
  try {
    const paper = await api<PaperSnapshot>(
      `/api/projects/${encodeURIComponent(profile.key.project)}/paper`,
      {
        method: "PUT",
        body: JSON.stringify({ content, base_hash: baseHash, draft_revision: draftRevision }),
      },
    );
    const previous = store.view<PaperResource>(id).value;
    mutation.settle(new Map([[id, { paper, sessions: previous?.sessions ?? [] }]]));
    return paper;
  } catch (error) {
    mutation.fail();
    if (error instanceof ApiError && error.status === 409 && error.code === "paper_draft_moved") {
      store.invalidate(id);
    }
    throw error;
  }
}

export async function createPaperDraft(
  store: ResourceStore,
  profile: ResourceProfile<PaperResource>,
): Promise<PaperSnapshot> {
  const id = resourceKeyId(profile.key);
  const mutation = store.beginMutation(
    `paper-create:${crypto.randomUUID()}`,
    new Map([[id, (value: unknown) => value]]),
  );
  try {
    const paper = await api<PaperSnapshot>(
      `/api/projects/${encodeURIComponent(profile.key.project)}/paper/create`,
      { method: "POST" },
    );
    mutation.settle(
      new Map([[id, { paper, sessions: store.view<PaperResource>(id).value?.sessions ?? [] }]]),
    );
    return paper;
  } catch (error) {
    mutation.fail();
    throw error;
  }
}
