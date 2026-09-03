export interface TextSpan {
  start: number;
  end: number;
}

export interface StagedChatAnnotation {
  id: string;
  selectedText: string;
  comment: string;
}

export interface ChatAnnotationAnchor {
  left: number;
  right: number;
  top: number;
}

export interface ChatAnnotationComposerPosition {
  left: number;
  top: number;
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
