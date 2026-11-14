import type { Box } from "../comments/box.ts";
import type { CommentAnchor, CommentRect } from "../comments/types.ts";
import {
  boundWhere,
  boxArea,
  markdownWhere,
  pdfWhere,
  plainTextWhere,
  previewWhere,
} from "../comments/where.ts";
import { surroundingText, MAX_QUOTE_CHARS } from "../comments/react/threadModel.ts";
import { textOffset } from "../comments/react/domText.ts";
import { pdfSourceNear } from "./previewComments";

const PAGE = ".paper-pdf-page[data-page]";

/** The first and last text nodes holding selected characters other than whitespace.
 *
 * A triple-click ends at the start of the next block, and a drag can start past the end
 * of a line; such a boundary holds none of the selected text, so it names no source. */
function selectedEnds(range: Range): [Text, Text] | null {
  const scope = range.commonAncestorContainer;
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  let first: Text | null = null;
  let last: Text | null = null;
  for (
    let node = scope instanceof Text ? scope : walker.nextNode();
    node instanceof Text;
    node = walker.nextNode()
  ) {
    if (!range.intersectsNode(node)) continue;
    const from = node === range.startContainer ? range.startOffset : 0;
    const to = node === range.endContainer ? range.endOffset : node.length;
    if (!node.data.slice(from, to).trim()) continue;
    first ??= node;
    last = node;
  }
  return first && last ? [first, last] : null;
}

/** Where the selection's last text ends on screen; a range across pages also spans the
 *  whole canvas of each page it passes, so its own bounds end at the page bottom. */
function endBottom(range: Range, last: Text) {
  const tail = document.createRange();
  tail.selectNodeContents(last);
  if (last === range.endContainer) tail.setEnd(last, range.endOffset);
  return tail.getBoundingClientRect().bottom;
}

/** A page slot's rect cut to `rect`, in fractions of the page; null when they miss. */
export function pageFraction(page: DOMRect, rect: CommentRect): CommentRect | null {
  const left = Math.max(page.left, rect.x);
  const top = Math.max(page.top, rect.y);
  const right = Math.min(page.right, rect.x + rect.width);
  const bottom = Math.min(page.bottom, rect.y + rect.height);
  if (right <= left || bottom <= top || !page.width || !page.height) return null;
  return {
    x: (left - page.left) / page.width,
    y: (top - page.top) / page.height,
    width: (right - left) / page.width,
    height: (bottom - top) / page.height,
  };
}

/** A selection inside the preview as a thread anchor: Markdown and plain text become
 *  text anchors on the rendered text, a PDF selection a pdf anchor on its first page. */
export function previewSelectionAnchor(
  selection: Selection,
  root: HTMLElement,
): { anchor: CommentAnchor; rect: DOMRect } | null {
  if (!selection.rangeCount || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const ends = selectedEnds(range);
  const start = ends?.[0].parentElement;
  const end = ends?.[1].parentElement;
  if (!start || !end || !root.contains(start) || !root.contains(end)) return null;
  const host = start.closest<HTMLElement>("[data-paper-path]");
  if (!host || !root.contains(host) || !host.contains(end)) return null;
  const rect = range.getBoundingClientRect();
  const firstPage = start.closest<HTMLElement>(PAGE);
  const lastPage = end.closest<HTMLElement>(PAGE);
  if (Boolean(firstPage) !== Boolean(lastPage)) return null;
  if (firstPage && lastPage) {
    const text = selection.toString().trim().slice(0, MAX_QUOTE_CHARS);
    const box = pageFraction(firstPage.getBoundingClientRect(), {
      x: rect.left,
      y: rect.top,
      width: rect.width,
      height: rect.height,
    });
    if (!text || !box) return null;
    const from = Number(firstPage.dataset.page);
    const to = Number(lastPage.dataset.page);
    return {
      anchor: {
        kind: "pdf",
        page: from,
        rect: box,
        text,
        comment: "",
        where: pdfWhere(
          host.dataset.paperPath!,
          from,
          to,
          undefined,
          pdfSourceNear(host, lastPage, endBottom(range, ends![1])),
        ),
      },
      rect,
    };
  }
  const quote = range.toString().trim().slice(0, MAX_QUOTE_CHARS);
  if (!quote) return null;
  const text = host.textContent ?? "";
  const at = text.indexOf(
    quote,
    Math.max(0, textOffset(host, range.startContainer, range.startOffset) - 1),
  );
  const where =
    host.tagName === "PRE" ? plainTextWhere(host, range) : markdownWhere(host, start, end);
  return {
    anchor: {
      kind: "text",
      text: quote,
      surrounding_text: at < 0 ? quote : surroundingText(text, at, at + quote.length),
      comment: "",
      where: previewWhere(where),
    },
    rect,
  };
}

/** The text-layer characters whose centers fall inside the box, in reading order.
 *
 * pdf.js often lays a whole line out as one run, so a box over part of a line is
 * judged character by character. */
export function boxText(target: HTMLElement, rect: DOMRect): string {
  return [...target.querySelectorAll<HTMLElement>(".textLayer span")]
    .map((span) => textInside(span, rect))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

function textInside(span: HTMLElement, rect: DOMRect): string {
  const run = span.firstChild;
  if (!(run instanceof Text) || !overlaps(span.getBoundingClientRect(), rect)) return "";
  const character = document.createRange();
  let kept = "";
  for (let index = 0; index < run.length; index++) {
    character.setStart(run, index);
    character.setEnd(run, index + 1);
    if (centerInside(character.getBoundingClientRect(), rect)) kept += run.data[index];
  }
  return kept;
}

const overlaps = (box: DOMRect, rect: DOMRect) =>
  box.right > rect.left && box.left < rect.right && box.bottom > rect.top && box.top < rect.bottom;

function centerInside(box: DOMRect, rect: DOMRect) {
  const x = box.left + box.width / 2;
  const y = box.top + box.height / 2;
  return box.width > 0 && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}

/** A box drawn over the PDF pages, in fractions of `surface`, as a pdf anchor on the
 *  page it starts on; null when it misses every page. */
export function pdfBoxAnchor(
  surface: HTMLElement,
  box: Box,
): { anchor: CommentAnchor; rect: DOMRect } | null {
  const bounds = surface.getBoundingClientRect();
  const rect = new DOMRect(
    bounds.left + box.x * bounds.width,
    bounds.top + box.y * bounds.height,
    box.width * bounds.width,
    box.height * bounds.height,
  );
  for (const page of surface.querySelectorAll<HTMLElement>(PAGE)) {
    const fraction = pageFraction(page.getBoundingClientRect(), rect);
    if (!fraction || rect.width < 4 || rect.height < 4) continue;
    const number = Number(page.dataset.page);
    const viewer = page.closest<HTMLElement>("[data-paper-path]");
    return {
      anchor: {
        kind: "pdf",
        page: number,
        rect: fraction,
        text: boxText(page, rect).slice(0, MAX_QUOTE_CHARS),
        comment: "",
        where: pdfWhere(
          viewer?.dataset.paperPath ?? "",
          number,
          number,
          boxArea(fraction),
          viewer ? pdfSourceNear(viewer, page, rect.bottom) : undefined,
        ),
      },
      rect,
    };
  }
  return null;
}

/** A box drawn on a Paper image, in fractions of the image. */
export function imageBoxAnchor(image: HTMLImageElement, path: string, box: Box): CommentAnchor {
  return {
    kind: "box",
    rect: box,
    viewport: {
      width: Math.max(1, Math.min(32768, image.naturalWidth || 1)),
      height: Math.max(1, Math.min(32768, image.naturalHeight || 1)),
    },
    elements: [],
    comment: "",
    where: boundWhere(`${path}, ${boxArea(box)}`),
  };
}
