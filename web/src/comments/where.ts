import type { CommentAnchor } from "./types.ts";

/** A location string an anchor carries: at most 256 characters. */
const MAX_WHERE = 256;
/** Marks a text anchor drawn in a preview rather than in the source it renders. */
const PREVIEW = " (preview)";

export function boundWhere(where: string): string {
  return where.length > MAX_WHERE ? where.slice(0, MAX_WHERE) : where;
}

/** `path:N` or `path:N-M`. */
export function linesWhere(path: string, from: number, to: number): string {
  return boundWhere(to > from ? `${path}:${from}-${to}` : `${path}:${from}`);
}

/** A preview's location, marked so the thread pins in the preview it was drawn in. */
export function previewWhere(where: string): string {
  return `${where.slice(0, MAX_WHERE - PREVIEW.length)}${PREVIEW}`;
}

/** Whether a thread was drawn in a preview: every kind but text is, and text says so. */
export function drawnInPreview(anchor: CommentAnchor): boolean {
  if (anchor.kind === "whole") return false;
  return anchor.kind !== "text" || Boolean(anchor.where?.endsWith(PREVIEW));
}

/** A box's place in fractions of its page, image, or frame: `area 10-40% across and 20-60% down`.
 *
 * Boxes send no pixels, so this is how an agent tells two boxes on one figure apart. */
export function boxArea(box: { x: number; y: number; width: number; height: number }): string {
  const span = (from: number, size: number) =>
    `${Math.round(from * 100)}-${Math.round((from + size) * 100)}%`;
  return `area ${span(box.x, box.width)} across and ${span(box.y, box.height)} down`;
}

/** Where a PDF selection or box sits: `root, page P` or `pages P-Q`, then its area, then
 *  the source line `near` names on its last page, which the length bound may cut. */
export function pdfWhere(
  path: string,
  from: number,
  to: number,
  area?: string,
  near?: string,
): string {
  const pages = from === to ? `page ${to}` : `pages ${from}-${to}`;
  return boundWhere(`${path}, ${pages}${area ? `, ${area}` : ""}${near ? `, near ${near}` : ""}`);
}

/** `path:N-M` from the source lines on the selection's Markdown elements; the path alone without them. */
export function markdownWhere(host: HTMLElement, start: Element, end: Element): string {
  const path = host.dataset.paperPath!;
  const first = start.closest<HTMLElement>("[data-line-start]");
  const last = end.closest<HTMLElement>("[data-line-end]");
  if (!first || !last || !host.contains(first) || !host.contains(last)) return boundWhere(path);
  return linesWhere(path, Number(first.dataset.lineStart), Number(last.dataset.lineEnd));
}

/** `path:N` from the newlines in a `<pre>` before the selection's trimmed ends. */
export function plainTextWhere(host: HTMLElement, range: Range): string {
  const text = range.toString();
  const before = host.ownerDocument.createRange();
  before.setStart(host, 0);
  before.setEnd(range.startContainer, range.startOffset);
  const full = before.toString() + text;
  const offset = before.toString().length;
  const lineAt = (index: number) => full.slice(0, index).split("\n").length;
  const from = lineAt(offset + text.length - text.trimStart().length);
  const to = lineAt(offset + text.trimEnd().length);
  return linesWhere(host.dataset.paperPath!, from, to);
}
