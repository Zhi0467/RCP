export interface TextSpan {
  start: number;
  end: number;
}

export interface StagedChatAnnotation {
  id: string;
  selectedText: string;
  comment: string;
}

export const MAX_CHAT_ANNOTATIONS = 12;
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
  height: number;
  bottom: number;
}

export interface ChatAnnotationTextControlSelection {
  value: string;
  selectionStart: number | null;
  selectionEnd: number | null;
}

const CHAT_ANNOTATION_COMPOSER_WIDTH = 320;
const CHAT_ANNOTATION_COMPOSER_HEIGHT = 228;
const CHAT_ANNOTATION_COMPOSER_GAP = 10;
const CHAT_ANNOTATION_VIEWPORT_MARGIN = 12;

export function replaceTextSpan(current: string, span: TextSpan, replacement: string) {
  return {
    value: `${current.slice(0, span.start)}${replacement}${current.slice(span.end)}`,
    end: span.start + replacement.length,
  };
}

export function assembleChatTurn(
  message: string,
  annotations: ReadonlyArray<Pick<StagedChatAnnotation, "selectedText" | "comment">>,
): string {
  const parts = [message.trim()];
  for (const annotation of annotations) {
    const selectedText = annotation.selectedText.trim();
    const comment = annotation.comment.trim();
    if (selectedText && comment) parts.push(`${selectedText}\ncomment: ${comment}`);
  }
  return parts.filter(Boolean).join("\n\n");
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
        candidate.comment.length > MAX_CHAT_ANNOTATION_COMMENT_LENGTH
      )
        return [];
      return [
        {
          id: candidate.id,
          selectedText: candidate.selectedText,
          comment: candidate.comment,
        },
      ];
    });
  } catch {
    return [];
  }
}

export function chatAnnotationTextControlSelection(
  control: ChatAnnotationTextControlSelection,
): string {
  const start = control.selectionStart;
  const end = control.selectionEnd;
  if (start === null || end === null || start === end) return "";
  return control.value.slice(start, end).trim();
}

export function chatAnnotationViewportMetrics(
  layoutViewportHeight: number,
  visualViewport: { height: number; offsetTop: number },
): ChatAnnotationViewportMetrics {
  return {
    height: Math.max(0, visualViewport.height),
    bottom: Math.max(0, layoutViewportHeight - visualViewport.offsetTop - visualViewport.height),
  };
}

export function chatAnnotationComposerPosition(
  anchor: ChatAnnotationAnchor,
  viewport: { width: number; height: number },
): ChatAnnotationComposerPosition {
  const rightPlacement = anchor.right + CHAT_ANNOTATION_COMPOSER_GAP;
  const leftPlacement = anchor.left - CHAT_ANNOTATION_COMPOSER_WIDTH - CHAT_ANNOTATION_COMPOSER_GAP;
  const availableRight = viewport.width - rightPlacement - CHAT_ANNOTATION_VIEWPORT_MARGIN;
  const left =
    availableRight >= CHAT_ANNOTATION_COMPOSER_WIDTH
      ? rightPlacement
      : leftPlacement >= CHAT_ANNOTATION_VIEWPORT_MARGIN
        ? leftPlacement
        : Math.max(
            CHAT_ANNOTATION_VIEWPORT_MARGIN,
            Math.min(
              anchor.left,
              viewport.width - CHAT_ANNOTATION_COMPOSER_WIDTH - CHAT_ANNOTATION_VIEWPORT_MARGIN,
            ),
          );
  const top = Math.max(
    CHAT_ANNOTATION_VIEWPORT_MARGIN,
    Math.min(
      anchor.top,
      viewport.height - CHAT_ANNOTATION_COMPOSER_HEIGHT - CHAT_ANNOTATION_VIEWPORT_MARGIN,
    ),
  );
  return { left, top };
}
