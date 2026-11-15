import { useSyncExternalStore } from "react";

/**
 * A comment link names one thread. App routes to the thread's target, then
 * leaves this request for the comment surface showing that target, which takes
 * it once its threads have loaded and calls `locateThread`. A newer link
 * replaces an untaken request.
 */
export interface CommentThreadRequest {
  projectId: string;
  targetKind: "artifact" | "paper" | "reply";
  targetId: string;
  threadId: string;
}

let request: CommentThreadRequest | null = null;
const listeners = new Set<() => void>();

function publish(next: CommentThreadRequest | null) {
  request = next;
  listeners.forEach((listener) => listener());
}

export function requestCommentThread(next: CommentThreadRequest): void {
  publish(next);
}

/** The requested thread id for this exact target, cleared as it is returned. */
export function takeCommentThreadRequest(
  projectId: string,
  targetKind: CommentThreadRequest["targetKind"],
  targetId: string,
): string | null {
  if (
    request?.projectId !== projectId ||
    request.targetKind !== targetKind ||
    request.targetId !== targetId
  )
    return null;
  const { threadId } = request;
  publish(null);
  return threadId;
}

/** Rerenders a mounted surface when a link arrives for a target it already shows. */
export function useCommentThreadRequest(): CommentThreadRequest | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    () => request,
    () => null,
  );
}
