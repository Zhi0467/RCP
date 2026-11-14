import type { CommentAnchor, CommentRect, CommentThread } from "./types.ts";

export interface AnchorContext {
  text: string;
  element(path: string): CommentRect | null;
  image?: CommentRect;
  page?(page: number): { bounds: CommentRect; text: string } | null;
  textRect(start: number, end: number, page?: number): CommentRect | null;
  sameVersion?: boolean;
  whole?: CommentRect;
}

/** `value` with each whitespace run read as one space, and where each kept character was. */
function collapse(value: string): { text: string; at: number[] } {
  let text = "";
  const at: number[] = [];
  for (let index = 0; index < value.length; index++) {
    const space = /\s/.test(value[index]);
    if (space && text.endsWith(" ")) continue;
    text += space ? " " : value[index];
    at.push(index);
  }
  return { text, at };
}

let collapsed: { source: string; value: ReturnType<typeof collapse> } | null = null;

/** Where `quote` is in `text`, preferring the copy inside `surrounding`. Whitespace
 *  runs match each other, so a quote survives reflowed or re-indented text. The
 *  artifact frame runs this same function from the bundle build. */
export function findText(
  text: string,
  quote: string,
  surrounding = "",
): { start: number; end: number } | null {
  const wanted = collapse(quote).text.trim();
  if (!wanted) return null;
  const around = collapse(surrounding).text.trim();
  if (collapsed?.source !== text) collapsed = { source: text, value: collapse(text) };
  const { text: haystack, at } = collapsed.value;
  const contextAt = around ? haystack.indexOf(around) : -1;
  const quoteAt = around.indexOf(wanted);
  const start = contextAt >= 0 && quoteAt >= 0 ? contextAt + quoteAt : haystack.indexOf(wanted);
  return start < 0 ? null : { start: at[start], end: at[start + wanted.length - 1] + 1 };
}

function fractionRect(bounds: CommentRect, region: CommentRect): CommentRect {
  return {
    x: bounds.x + region.x * bounds.width,
    y: bounds.y + region.y * bounds.height,
    width: region.width * bounds.width,
    height: region.height * bounds.height,
  };
}

export function findAnchor(anchor: CommentAnchor, context: AnchorContext): CommentRect | null {
  switch (anchor.kind) {
    case "whole":
      return context.whole ?? null;
    case "element":
      return context.element(anchor.path);
    case "text": {
      const match = findText(context.text, anchor.text, anchor.surrounding_text);
      return match && context.textRect(match.start, match.end);
    }
    case "box": {
      const element = anchor.elements?.[0];
      if (!element) return context.image ? fractionRect(context.image, anchor.rect) : null;
      const bounds = context.element(element.path);
      return bounds && (element.region ? fractionRect(bounds, element.region) : bounds);
    }
    case "pdf": {
      const page = context.page?.(anchor.page);
      if (!page) return null;
      if (context.sameVersion !== false && anchor.rect)
        return fractionRect(page.bounds, anchor.rect);
      const match = findText(page.text, anchor.text);
      return match && context.textRect(match.start, match.end, anchor.page);
    }
  }
}

export function locateThread(
  thread: Pick<CommentThread, "anchor">,
  context: AnchorContext & { scroll(rect: CommentRect): void; flash(rect: CommentRect): void },
): boolean {
  const rect = findAnchor(thread.anchor, context);
  if (!rect) return false;
  context.scroll(rect);
  context.flash(rect);
  return true;
}
