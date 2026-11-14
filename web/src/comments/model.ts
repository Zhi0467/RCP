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

export function railState(
  threads: readonly CommentThread[],
  ticks: ReadonlySet<string>,
  typedText = "",
  maxSelections = 50,
) {
  const open = threads.filter((thread) => thread.state === "open");
  const resolved = threads.filter((thread) => thread.state === "resolved");
  const selected = open.filter((thread) => thread.can_send && ticks.has(thread.thread_id));
  const ticked = new Set(selected.map((thread) => thread.thread_id));
  const sendLength =
    typedText.length +
    selected.reduce(
      (sum, thread) =>
        sum +
        anchorQuote(thread.anchor).length +
        thread.posts.reduce((length, post) => length + post.text.length, 0),
      0,
    );
  return {
    open,
    resolved,
    selected,
    ticked,
    sendLength,
    canSend: selected.length > 0 && selected.length <= maxSelections && sendLength <= 32000,
  };
}
