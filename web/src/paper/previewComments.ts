import type { Element as HastElement, ElementContent, Root } from "hast";
import type { ChatAnnotationAnchor } from "../chat/chatInput";
import { synctexSource, type SyncTexMap } from "./synctex";

/** A commentable selection in the Paper preview: the range shown, its quote, and its source. */
export interface PreviewSelection {
  range: Range;
  quote: string;
  where?: string;
}

/** A comment the preview opens itself: a box on a PDF page or image, or an HTML frame selection. */
export interface PreviewDirectComment {
  quote: string;
  where?: string;
  anchor: ChatAnnotationAnchor;
  /** Clears the selection drawn for it; called when the comment box closes. */
  clear?: () => void;
}

// The DOM carries everything a resolver needs, so it reads no React state:
// `data-paper-path` names the shown file (or compile root) on the preview's
// container, Markdown elements carry their source lines, and each PDF page slot
// carries its page number and width in points.
const synctexMaps = new WeakMap<Element, SyncTexMap>();

/** Gives a PDF viewer element its compile's SyncTeX map, or removes it. */
export function registerPdfSynctex(viewer: Element, map: SyncTexMap | undefined) {
  if (map && Object.keys(map).length) synctexMaps.set(viewer, map);
  else synctexMaps.delete(viewer);
}

/** Copies each element's source lines to `data-line-start`/`data-line-end`; runs before `rehype-katex`. */
export function rehypeSourceLines() {
  return (tree: Root) => {
    const visit = (parent: { children: ElementContent[] }) => {
      parent.children = parent.children.map((child) => {
        if (child.type !== "element") return child;
        visit(child);
        if (!child.position) return child;
        const lines = {
          dataLineStart: child.position.start.line,
          dataLineEnd: child.position.end.line,
        };
        // KaTeX replaces display math with its own markup; a block around it keeps the lines.
        if (isDisplayMath(child))
          return { type: "element", tagName: "div", properties: lines, children: [child] };
        child.properties = { ...child.properties, ...lines };
        return child;
      });
    };
    visit(tree as unknown as { children: ElementContent[] });
  };
}

function isDisplayMath(element: HastElement) {
  return (
    element.tagName === "pre" &&
    element.children.some(
      (child) =>
        child.type === "element" &&
        child.tagName === "code" &&
        String(child.properties.className ?? "").includes("math"),
    )
  );
}

/** Where a PDF selection or box sits: `root, page P` or `pages P-Q`, near a LaTeX line on its last page. */
function pdfWhere(
  first: HTMLElement,
  last: HTMLElement,
  bottom: number,
  area?: string,
): string | undefined {
  const viewer = last.closest<HTMLElement>("[data-paper-path]");
  if (!viewer) return undefined;
  const from = Number(first.dataset.page);
  const to = Number(last.dataset.page);
  const pages = from === to ? `page ${to}` : `pages ${from}-${to}`;
  // The area precedes the approximate source line, which the length limit may cut.
  const where = `${viewer.dataset.paperPath}, ${pages}${area ? `, ${area}` : ""}`;
  const map = synctexMaps.get(viewer);
  const points = Number(last.dataset.points);
  if (!map || !points || !last.clientWidth) return where;
  // The inverse of the scale PaperPdf scrolls with: CSS pixels on the slot to points.
  const y = ((bottom - last.getBoundingClientRect().top) * points) / last.clientWidth;
  const source = synctexSource(map, to, y);
  return source ? `${where}, near ${source.path} line ${source.line}` : where;
}

/** A box's place in fractions of its page, image, or frame: `area 10-40% across and 20-60% down`.
 *
 * Boxes send no pixels, so this is how the coach tells two boxes on one figure apart. */
export function boxArea(box: { x: number; y: number; width: number; height: number }) {
  const span = (from: number, size: number) =>
    `${Math.round(from * 100)}-${Math.round((from + size) * 100)}%`;
  return `area ${span(box.x, box.width)} across and ${span(box.y, box.height)} down`;
}

/** Where a box drawn on a PDF page slot or an image sits, including its area. */
export function boxWhere(target: HTMLElement, rect: DOMRect): string | undefined {
  const bounds = target.getBoundingClientRect();
  const area = boxArea({
    x: (rect.left - bounds.left) / bounds.width,
    y: (rect.top - bounds.top) / bounds.height,
    width: rect.width / bounds.width,
    height: rect.height / bounds.height,
  });
  if (target.dataset.page) return pdfWhere(target, target, rect.bottom, area);
  const path = target.closest<HTMLElement>("[data-paper-path]")?.dataset.paperPath;
  return path && `${path}, ${area}`;
}

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

/** The commentable part of a DOM selection inside the preview root, or null. */
export function resolvePreviewSelection(
  selection: Selection,
  root: HTMLElement,
): PreviewSelection | null {
  if (!selection.rangeCount || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  // The browser's own reading keeps line and block breaks; Range.toString runs the
  // lines of a PDF text layer together.
  const quote = selection.toString().trim();
  const ends = selectedEnds(range);
  const start = ends?.[0].parentElement;
  const end = ends?.[1].parentElement;
  if (!quote || !start || !end || !root.contains(start) || !root.contains(end)) return null;
  const host = start.closest<HTMLElement>("[data-paper-path]");
  if (!host || !root.contains(host) || !host.contains(end)) return null;
  const firstPage = start.closest<HTMLElement>(".paper-pdf-page[data-page]");
  const lastPage = end.closest<HTMLElement>(".paper-pdf-page[data-page]");
  if (Boolean(firstPage) !== Boolean(lastPage)) return null;
  const where =
    firstPage && lastPage
      ? pdfWhere(firstPage, lastPage, endBottom(range, ends![1]))
      : host.tagName === "PRE"
        ? plainTextWhere(host, range)
        : markdownWhere(host, start, end);
  return { range: range.cloneRange(), quote, where };
}

/** Where the selection's last text ends on screen; a range across pages also spans the
 * whole canvas of each page it passes, so its own bounds end at the page bottom. */
function endBottom(range: Range, last: Text) {
  const tail = document.createRange();
  tail.selectNodeContents(last);
  if (last === range.endContainer) tail.setEnd(last, range.endOffset);
  return tail.getBoundingClientRect().bottom;
}

/** `path, line N` from the newlines in the `<pre>` before the selection's trimmed ends. */
function plainTextWhere(host: HTMLElement, range: Range) {
  const text = range.toString();
  const before = document.createRange();
  before.setStart(host, 0);
  before.setEnd(range.startContainer, range.startOffset);
  const full = before.toString() + text;
  const offset = before.toString().length;
  const lineAt = (index: number) => full.slice(0, index).split("\n").length;
  const from = lineAt(offset + text.length - text.trimStart().length);
  const to = lineAt(offset + text.trimEnd().length);
  return linesWhere(host.dataset.paperPath!, from, to);
}

/** `path, line N` from the source lines on the selection's Markdown elements; the path alone without them. */
function markdownWhere(host: HTMLElement, start: Element, end: Element) {
  const path = host.dataset.paperPath!;
  const first = start.closest<HTMLElement>("[data-line-start]");
  const last = end.closest<HTMLElement>("[data-line-end]");
  if (!first || !last || !host.contains(first) || !host.contains(last)) return path;
  return linesWhere(path, Number(first.dataset.lineStart), Number(last.dataset.lineEnd));
}

function linesWhere(path: string, from: number, to: number) {
  return to > from ? `${path}, lines ${from}-${to}` : `${path}, line ${from}`;
}
