import * as Y from "yjs";
import type { PaperChange } from "./PaperChanges";

type Anchor = { start: Y.RelativePosition; end: Y.RelativePosition };

function matches(change: PaperChange, text: Y.Text, start: number, end: number) {
  return (
    Number.isInteger(start) &&
    Number.isInteger(end) &&
    start >= 0 &&
    end >= start &&
    end <= text.length &&
    text.toString().slice(start, end) === change.current?.text
  );
}

export function anchorPaperChange(change: PaperChange, text: Y.Text): Anchor | null {
  const current = change.current;
  if (!current || !matches(change, text, current.start, current.end)) return null;
  return {
    start: Y.createRelativePositionFromTypeIndex(text, current.start),
    end: Y.createRelativePositionFromTypeIndex(text, current.end),
  };
}

export function resolvePaperAnchor(anchor: Anchor, change: PaperChange, text: Y.Text) {
  if (!text.doc) return null;
  const start = Y.createAbsolutePositionFromRelativePosition(anchor.start, text.doc);
  const end = Y.createAbsolutePositionFromRelativePosition(anchor.end, text.doc);
  if (
    !start ||
    !end ||
    start.type !== text ||
    end.type !== text ||
    !matches(change, text, start.index, end.index)
  )
    return null;
  return { from: start.index, to: end.index };
}
