/** One LaTeX compile's SyncTeX map: per tree path, `[line, page, y points from the page top]` by line. */
export type SyncTexMap = Record<string, [number, number, number][]>;
export type PdfSpot = { page: number; y: number };

/** Where a source line sits in the PDF: its own mark, else the nearest earlier line's, else the file's first. */
export function synctexSpot(map: SyncTexMap, path: string, line: number): PdfSpot | null {
  const marks = map[path];
  if (!marks?.length) return null;
  let found = marks[0];
  for (const mark of marks) {
    if (mark[0] > line) break;
    found = mark;
  }
  return { page: found[1], y: found[2] };
}
