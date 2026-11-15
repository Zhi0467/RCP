import { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type {
  CreateShareLinkRequest,
  GraphTargetRef,
  ProjectReferenceSelector,
  ShareLinkResponse,
} from "./types";

type Fetcher = (path: string, init: RequestInit) => Promise<Response>;
type Poster = (path: string, init: RequestInit) => Promise<ShareLinkResponse>;

export function shareLinkGraphTarget(target: GraphTargetRef): string {
  return target.kind === "branch" ? `branch:${target.branch_id}` : "main";
}

/** The share-link request for what Copy reference names, on the same graph target. */
export function shareLinkRequest(
  target: GraphTargetRef,
  selector: ProjectReferenceSelector,
): CreateShareLinkRequest {
  if (selector.kind === "node") {
    return {
      kind: "node",
      item_id: selector.node_id,
      graph_target: selector.branch_id ? `branch:${selector.branch_id}` : "main",
    };
  }
  if (selector.kind === "artifact") {
    return {
      kind: "artifact",
      item_id: selector.artifact_id,
      graph_target: shareLinkGraphTarget(target),
    };
  }
  return { kind: "paper", item_id: selector.file_id ?? "", graph_target: "main" };
}

/** Whether this space hands out share links: a team space with an access URL. */
export async function shareLinksEnabled(fetcher: Fetcher = fetch): Promise<boolean> {
  const health = await fetcher("/api/health", { credentials: "same-origin" });
  if (!health.ok || ((await health.json()) as { space_kind?: string }).space_kind !== "team") {
    return false;
  }
  const response = await fetcher("/api/team/space", { credentials: "same-origin" });
  if (!response.ok) return false;
  const space = (await response.json()) as { access_url?: string | null };
  return Boolean(space.access_url);
}

let enabled: Promise<boolean> | null = null;
let known = false;

export function useShareLinks(): boolean {
  const [on, setOn] = useState(known);
  useEffect(() => {
    let live = true;
    enabled ??= shareLinksEnabled().then(
      (value) => (known = value),
      () => {
        enabled = null;
        return false;
      },
    );
    void enabled.then((value) => live && setOn(value));
    return () => {
      live = false;
    };
  }, []);
  return on;
}

const postShareLink: Poster = (path, init) => api<ShareLinkResponse>(path, init);

/**
 * Copies an item's share link. WebKit drops a clipboard write made after an
 * `await`, so the write starts inside the click with the URL still pending.
 */
export async function copyShareLink(
  projectId: string,
  request: CreateShareLinkRequest,
  {
    post = postShareLink,
    clipboard = navigator.clipboard,
    Item = ClipboardItem,
  }: {
    post?: Poster;
    clipboard?: Pick<Clipboard, "write">;
    Item?: typeof ClipboardItem;
  } = {},
): Promise<void> {
  const url = post(`/api/projects/${encodeURIComponent(projectId)}/share-links`, {
    method: "POST",
    body: JSON.stringify(request),
  }).then(
    (body) => body.url,
    (error: unknown) => {
      throw error instanceof ApiError && error.status === 404
        ? new Error("This item cannot be linked.")
        : error;
    },
  );
  const written = clipboard.write([
    new Item({ "text/plain": url.then((value) => new Blob([value], { type: "text/plain" })) }),
  ]);
  const [link, write] = await Promise.allSettled([url, written]);
  if (link.status === "rejected") throw link.reason;
  if (write.status === "rejected") {
    throw new Error("Could not copy the link. Check clipboard access and try again.");
  }
}
