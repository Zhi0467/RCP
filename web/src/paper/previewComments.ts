import type { Element as HastElement, ElementContent, Root } from "hast";
import { decodeNamedCharacterReference } from "decode-named-character-reference";
import { synctexSource, type SyncTexMap } from "./synctex";

// The DOM carries what source lookups need: `data-paper-path` names the shown file
// (or compile root) on the preview's container, Markdown elements carry their source
// lines, and each PDF page slot carries its page number and width in points.
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

/** The source line near a point on a PDF page slot, from its compile's SyncTeX map:
 *  `path line N`, or undefined without a map. */
export function pdfSourceNear(
  viewer: HTMLElement,
  page: HTMLElement,
  clientY: number,
): string | undefined {
  const map = synctexMaps.get(viewer);
  const points = Number(page.dataset.points);
  if (!map || !points || !page.clientWidth) return undefined;
  // The inverse of the scale PaperPdf scrolls with: CSS pixels on the slot to points.
  const y = ((clientY - page.getBoundingClientRect().top) * points) / page.clientWidth;
  const source = synctexSource(map, Number(page.dataset.page), y);
  return source ? `${source.path} line ${source.line}` : undefined;
}

/** A double-clicked preview point in its source: the file, the lines it renders from, and
 * which occurrence of the clicked word within those lines it is. */
export interface PreviewSourceSpot {
  path: string;
  from: number;
  to: number;
  word: string;
  occurrence: number;
}

/** Where a double-click on the preview points in the source, from the same marks comments use. */
export function previewSourceSpot(
  target: Element,
  selection: Selection | null,
  clientY: number,
): PreviewSourceSpot | null {
  const host = target.closest<HTMLElement>("[data-paper-path]");
  if (!host) return null;
  const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
  const word = range?.toString().trim() ?? "";
  const page = target.closest<HTMLElement>(".paper-pdf-page[data-page]");
  if (page) {
    const map = synctexMaps.get(host);
    const points = Number(page.dataset.points);
    if (!map || !points || !page.clientWidth) return null;
    const y = ((clientY - page.getBoundingClientRect().top) * points) / page.clientWidth;
    const source = synctexSource(map, Number(page.dataset.page), y);
    return source && { path: source.path, from: source.line, to: source.line, word, occurrence: 0 };
  }
  const path = host.dataset.paperPath!;
  if (!range || !host.contains(range.startContainer)) return null;
  if (host.tagName === "PRE") {
    const lines = textBefore(host, range).split("\n");
    return {
      path,
      from: lines.length,
      to: lines.length,
      word,
      occurrence: count(lines.at(-1)!, word),
    };
  }
  const block = target.closest<HTMLElement>("[data-line-start]");
  if (!block || !host.contains(block)) return null;
  return {
    path,
    from: Number(block.dataset.lineStart),
    to: Number(block.dataset.lineEnd),
    word,
    occurrence: count(textBefore(block, range), word),
  };
}

/** Where a word double-clicked in an HTML page sits in its source: the page reports the
 * word and how often its text showed the word before, and the same count over the source
 * with its markup blanked finds it. */
export function htmlSourceSpot(
  path: string,
  source: string,
  word: string,
  occurrence: number,
): PreviewSourceSpot | null {
  const blank = (markup: string) => markup.replace(/[^\n]/g, " ");
  const body = source.search(/<body[\s>]/i);
  let text = source.replace(
    /<!--[\s\S]*?-->|<(script|style|template|noscript)\b[\s\S]*?<\/\1\s*>|<[^>]*>/gi,
    blank,
  );
  if (body > 0) text = blank(text.slice(0, body)) + text.slice(body);
  const shown = decodeReferences(text);
  let found = -1;
  for (let seen = 0; word && seen <= occurrence; seen++) {
    const next = shown.text.indexOf(word, found + 1);
    if (next < 0) break;
    found = next;
  }
  if (found < 0) return null;
  const at = shown.source[found];
  const line = source.slice(0, at).split("\n").length;
  const lineStart = source.lastIndexOf("\n", at - 1) + 1;
  return { path, from: line, to: line, word, occurrence: count(source.slice(lineStart, at), word) };
}

function referenceValue(name: string): string | false {
  if (!name.startsWith("#")) return decodeNamedCharacterReference(name);
  const hex = name[1] === "x" || name[1] === "X";
  const code = Number.parseInt(name.slice(hex ? 2 : 1), hex ? 16 : 10);
  return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : false;
}

/** The text with its character references decoded, and each character's source offset. */
function decodeReferences(text: string) {
  let decoded = "";
  const source: number[] = [];
  let last = 0;
  const keep = (end: number) => {
    for (let index = last; index < end; index++) source.push(index);
    decoded += text.slice(last, end);
  };
  for (const match of text.matchAll(/&(#x[0-9a-f]+|#[0-9]+|[a-z][a-z0-9]*);/gi)) {
    const value = referenceValue(match[1]);
    if (value === false) continue;
    keep(match.index);
    for (let index = 0; index < value.length; index++) source.push(match.index);
    decoded += value;
    last = match.index + match[0].length;
  }
  keep(text.length);
  return { text: decoded, source };
}

function textBefore(container: Node, range: Range) {
  const before = document.createRange();
  before.setStart(container, 0);
  before.setEnd(range.startContainer, range.startOffset);
  return before.toString();
}

function count(text: string, word: string) {
  return word ? text.split(word).length - 1 : 0;
}

/** The source range a spot names: its word's occurrence within its lines, else the first
 * line's start. Markup the preview hides can shift occurrences, so a missing occurrence
 * falls back to the word's last appearance in those lines. */
export function sourceSpotRange(
  text: string,
  spot: PreviewSourceSpot,
): { from: number; to: number } {
  const lines = text.split("\n");
  const first = Math.min(Math.max(spot.from, 1), lines.length);
  const start = lines.slice(0, first - 1).reduce((sum, line) => sum + line.length + 1, 0);
  const span = lines.slice(first - 1, Math.max(spot.to, first)).join("\n");
  let at = -1;
  for (let seen = 0; spot.word && seen <= spot.occurrence; seen++) {
    const next = span.indexOf(spot.word, at + 1);
    if (next < 0) break;
    at = next;
  }
  if (at < 0) return { from: start, to: start };
  return { from: start + at, to: start + at + spot.word.length };
}
