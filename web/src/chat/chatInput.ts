export interface TextSpan {
  start: number;
  end: number;
}

export interface ChatAnnotationComposerPosition {
  left: number;
  top: number;
}

export interface ChatAnnotationViewportMetrics {
  left: number;
  top: number;
  width: number;
  height: number;
  right: number;
  bottom: number;
}

const CHAT_ANNOTATION_VIEWPORT_MARGIN = 12;
// Close enough to read as part of the selection.
const CHAT_SELECTION_COMMENT_GAP = 6;

export function replaceTextSpan(current: string, span: TextSpan, replacement: string) {
  return {
    value: `${current.slice(0, span.start)}${replacement}${current.slice(span.end)}`,
    end: span.start + replacement.length,
  };
}

/**
 * The selected range clamped to the annotatable answer where the selection began,
 * or null when the selection did not begin inside an answer under `root`. A sweep
 * that lifts past the answer's edge into neighbouring controls keeps the answer text.
 */
export function annotatableAnswerSelectionRange(
  selection: Selection | null,
  root: Element | null,
): Range | null {
  if (!root || !selection || selection.isCollapsed || selection.rangeCount !== 1) return null;
  const anchor = selection.anchorNode;
  if (!anchor) return null;
  const origin = anchor instanceof Element ? anchor : anchor.parentElement;
  const answer = origin?.closest<HTMLElement>(".chat-annotatable-answer") ?? null;
  if (!answer || !root.contains(answer)) return null;
  const range = selection.getRangeAt(0).cloneRange();
  if (!answer.contains(range.startContainer)) range.setStart(answer, 0);
  if (!answer.contains(range.endContainer)) range.setEnd(answer, answer.childNodes.length);
  return range.collapsed ? null : range;
}

export function chatAnnotationViewportMetrics(
  layoutViewport: { width: number; height: number },
  visualViewport?: {
    width: number;
    height: number;
    offsetLeft: number;
    offsetTop: number;
  } | null,
): ChatAnnotationViewportMetrics {
  const left = Math.max(0, visualViewport?.offsetLeft ?? 0);
  const top = Math.max(0, visualViewport?.offsetTop ?? 0);
  const width = Math.max(
    0,
    Math.min(visualViewport?.width ?? layoutViewport.width, layoutViewport.width - left),
  );
  const height = Math.max(
    0,
    Math.min(visualViewport?.height ?? layoutViewport.height, layoutViewport.height - top),
  );
  return {
    left,
    top,
    width,
    height,
    right: Math.max(0, layoutViewport.width - left - width),
    bottom: Math.max(0, layoutViewport.height - top - height),
  };
}

/**
 * Where the selection's Comment button goes: directly on top of the selection,
 * centred over its first line, flipped below its last line when there is no
 * room above, and always inside the visible viewport.
 */
export function chatSelectionCommentPosition(
  selection: {
    firstLine: { left: number; right: number; top: number };
    bottom: number;
  },
  viewport: Pick<ChatAnnotationViewportMetrics, "left" | "top" | "width" | "height">,
  button: { width: number; height: number },
): ChatAnnotationComposerPosition {
  const viewportRight = viewport.left + viewport.width;
  const viewportBottom = viewport.top + viewport.height;
  const above = selection.firstLine.top - button.height - CHAT_SELECTION_COMMENT_GAP;
  const below = selection.bottom + CHAT_SELECTION_COMMENT_GAP;
  const top = above >= viewport.top + CHAT_ANNOTATION_VIEWPORT_MARGIN ? above : below;
  const centre = (selection.firstLine.left + selection.firstLine.right) / 2;
  const left = Math.max(
    viewport.left + CHAT_ANNOTATION_VIEWPORT_MARGIN,
    Math.min(
      centre - button.width / 2,
      viewportRight - button.width - CHAT_ANNOTATION_VIEWPORT_MARGIN,
    ),
  );
  return {
    left,
    top: Math.max(
      viewport.top + CHAT_ANNOTATION_VIEWPORT_MARGIN,
      Math.min(top, viewportBottom - button.height - CHAT_ANNOTATION_VIEWPORT_MARGIN),
    ),
  };
}
