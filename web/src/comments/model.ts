import type { CommentAnchor, CommentThread } from "./types.ts";

export function anchorQuote(anchor: CommentAnchor): string {
  if (anchor.kind === "whole") return "";
  if (anchor.kind === "box")
    return (
      anchor.elements?.map((element) => element.label || element.text || element.path).join(", ") ||
      anchor.labels ||
      ""
    );
  return anchor.text;
}

/** A thread list's open threads and its Resolved fold. */
export function railState(threads: readonly CommentThread[]) {
  return {
    open: threads.filter((thread) => thread.state === "open"),
    resolved: threads.filter((thread) => thread.state === "resolved"),
  };
}
