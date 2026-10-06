export interface TextSpan {
  start: number;
  end: number;
}

import type { ArtifactContextRequest, ArtifactSelection } from "../core/types";

/** One staged comment: what the human picked, and what they said about it. */
export interface StagedChatAnnotation {
  id: string;
  /** What the comment is about as the human sees it: copied answer text, or an
   * artifact selection's description. */
  selectedText: string;
  comment: string;
  /** Set when the comment is on an artifact selection, which the server stages. */
  artifact?: StagedArtifactTarget;
}

export interface StagedArtifactTarget {
  context: Omit<ArtifactContextRequest, "selections">;
  name: string;
  selection: ArtifactSelection;
}

export const MAX_CHAT_ANNOTATIONS = 50;
// Mirrors ARTIFACT_CONTEXT_MAX_SELECTIONS in src/rcp/limits.py.
export const MAX_ARTIFACT_SELECTIONS = 50;
export const MAX_CHAT_ANNOTATION_TEXT_LENGTH = 4096;
export const MAX_CHAT_ANNOTATION_COMMENT_LENGTH = 2048;

export interface ChatAnnotationAnchor {
  left: number;
  right: number;
  top: number;
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

export interface ChatAnnotationTextControlSelection {
  value: string;
  selectionStart: number | null;
  selectionEnd: number | null;
}

const CHAT_ANNOTATION_COMPOSER_GAP = 10;
const CHAT_ANNOTATION_VIEWPORT_MARGIN = 12;
// Clears the handle a touch platform draws at either end of a selection.
const CHAT_SELECTION_COMMENT_GAP = 14;

export function replaceTextSpan(current: string, span: TextSpan, replacement: string) {
  return {
    value: `${current.slice(0, span.start)}${replacement}${current.slice(span.end)}`,
    end: span.start + replacement.length,
  };
}

export function assembleChatTurn(
  message: string,
  annotations: ReadonlyArray<Pick<StagedChatAnnotation, "selectedText" | "comment" | "artifact">>,
): string {
  const parts = [message.trim()];
  // Artifact comments travel as the turn's artifact selections; the server writes
  // them into the turn in the one shape every artifact route uses.
  for (const annotation of annotations) {
    const selectedText = annotation.selectedText.trim();
    const comment = annotation.comment.trim();
    if (annotation.artifact || !selectedText || !comment) continue;
    parts.push(`${selectedText}\ncomment: ${comment}`);
  }
  return parts.filter(Boolean).join("\n\n");
}

/** The artifact selections a turn carries, in the order their comments are numbered. */
export function stagedArtifactContext(
  annotations: ReadonlyArray<StagedChatAnnotation>,
): ArtifactContextRequest | null {
  const targeted = annotations.filter((annotation) => annotation.artifact);
  const first = targeted[0]?.artifact;
  if (!first) return null;
  return {
    ...first.context,
    selections: targeted.map((annotation) => ({
      ...annotation.artifact!.selection,
      comment: annotation.comment.trim(),
    })),
  };
}

export function stagedChatAnnotationsAreComplete(
  annotations: ReadonlyArray<Pick<StagedChatAnnotation, "selectedText" | "comment">>,
): boolean {
  return annotations.every(
    (annotation) => Boolean(annotation.selectedText.trim()) && Boolean(annotation.comment.trim()),
  );
}

export function parseStagedChatAnnotations(raw: string | null): StagedChatAnnotation[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length > MAX_CHAT_ANNOTATIONS) return [];
    return parsed.flatMap((item) => {
      if (!item || typeof item !== "object" || Array.isArray(item)) return [];
      const candidate = item as Record<string, unknown>;
      if (
        typeof candidate.id !== "string" ||
        candidate.id.length < 1 ||
        candidate.id.length > 128 ||
        typeof candidate.selectedText !== "string" ||
        candidate.selectedText.trim().length < 1 ||
        candidate.selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH ||
        typeof candidate.comment !== "string" ||
        candidate.comment.length > MAX_CHAT_ANNOTATION_COMMENT_LENGTH ||
        (candidate.artifact !== undefined && !isStagedArtifactTarget(candidate.artifact))
      )
        return [];
      return [
        {
          id: candidate.id,
          selectedText: candidate.selectedText,
          comment: candidate.comment,
          ...(candidate.artifact ? { artifact: candidate.artifact as StagedArtifactTarget } : {}),
        },
      ];
    });
  } catch {
    return [];
  }
}

// This tab wrote the draft itself; the server validates the selection it receives.
function isStagedArtifactTarget(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  const target = value as Partial<StagedArtifactTarget>;
  return (
    typeof target.name === "string" &&
    typeof target.context?.operation_id === "string" &&
    typeof target.context.artifact_id === "string" &&
    (target.selection?.kind === "text" || target.selection?.kind === "box")
  );
}

export function chatAnnotationTextControlSelection(
  control: ChatAnnotationTextControlSelection,
): string {
  const start = control.selectionStart;
  const end = control.selectionEnd;
  if (start === null || end === null || start === end) return "";
  return control.value.slice(start, end).trim();
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

export function chatAnnotationComposerPosition(
  anchor: ChatAnnotationAnchor,
  viewport: Pick<ChatAnnotationViewportMetrics, "left" | "top" | "width" | "height">,
  composer: { width: number; height: number },
): ChatAnnotationComposerPosition {
  const viewportRight = viewport.left + viewport.width;
  const viewportBottom = viewport.top + viewport.height;
  const rightPlacement = anchor.right + CHAT_ANNOTATION_COMPOSER_GAP;
  const leftPlacement = anchor.left - composer.width - CHAT_ANNOTATION_COMPOSER_GAP;
  const left =
    rightPlacement + composer.width <= viewportRight - CHAT_ANNOTATION_VIEWPORT_MARGIN
      ? rightPlacement
      : leftPlacement >= viewport.left + CHAT_ANNOTATION_VIEWPORT_MARGIN
        ? leftPlacement
        : Math.max(
            viewport.left + CHAT_ANNOTATION_VIEWPORT_MARGIN,
            Math.min(anchor.left, viewportRight - composer.width - CHAT_ANNOTATION_VIEWPORT_MARGIN),
          );
  const top = Math.max(
    viewport.top + CHAT_ANNOTATION_VIEWPORT_MARGIN,
    Math.min(anchor.top, viewportBottom - composer.height - CHAT_ANNOTATION_VIEWPORT_MARGIN),
  );
  return { left, top };
}
