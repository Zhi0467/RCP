import type { Point } from "../box.ts";
import type { CommentRect } from "../types.ts";

/** The range over characters `start`–`end` of `root`'s text, counted as `textContent` counts. */
export function textRange(root: Node, start: number, end: number): Range | null {
  const walker = root.ownerDocument!.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = root.ownerDocument!.createRange();
  let offset = 0;
  let started = false;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node as Text;
    const next = offset + text.length;
    if (!started && start < next) {
      range.setStart(text, start - offset);
      started = true;
    }
    if (started && end <= next) {
      range.setEnd(text, end - offset);
      return range;
    }
    offset = next;
  }
  return null;
}

/** Where `node`'s text begins within `root`'s text. */
export function textOffset(root: Node, node: Node, offset: number): number {
  const before = root.ownerDocument!.createRange();
  before.setStart(root, 0);
  before.setEnd(node, offset);
  return before.toString().length;
}

export function toRect(
  rect: DOMRect | { left: number; top: number; width: number; height: number },
) {
  return { x: rect.left, y: rect.top, width: rect.width, height: rect.height };
}

/** Where a pin sits for a range: the end of its last line. */
export function rangeEnd(range: Range): Point | null {
  const rects = [...range.getClientRects()].filter((rect) => rect.width || rect.height);
  const last = rects.at(-1);
  return last ? { x: last.right, y: last.top + last.height / 2 } : null;
}

/** A rect's end, for pins on boxes and pages: its top-right corner. */
export function rectEnd(rect: CommentRect): Point {
  return { x: rect.x + rect.width, y: rect.y };
}

/** Whether a point shows inside an element's visible box (a hidden pane has none). */
export function insideElement(point: Point, element: Element | null | undefined): boolean {
  const box = element?.getBoundingClientRect();
  return Boolean(
    box?.width &&
    box.height &&
    point.x >= box.left - 1 &&
    point.x <= box.right + 1 &&
    point.y >= box.top &&
    point.y <= box.bottom,
  );
}

/** Flashes a rect in the window, the shared locate mark. */
export function flashRect(rect: CommentRect) {
  const mark = document.createElement("div");
  mark.className = "rcp-comment-flash";
  Object.assign(mark.style, {
    left: `${rect.x}px`,
    top: `${rect.y}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
  });
  document.body.append(mark);
  mark.addEventListener("animationend", () => mark.remove(), { once: true });
  setTimeout(() => mark.remove(), 1200);
}
