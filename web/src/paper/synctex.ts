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

/** The source line nearest above a PDF point: across every path, the mark on `page` with the largest `y` at or above it. */
export function synctexSource(
  map: SyncTexMap,
  page: number,
  y: number,
): { path: string; line: number } | null {
  let found: { path: string; line: number; y: number } | null = null;
  for (const [path, marks] of Object.entries(map)) {
    for (const [line, markPage, markY] of marks) {
      if (markPage === page && markY <= y && (!found || markY > found.y))
        found = { path, line, y: markY };
    }
  }
  return found && { path: found.path, line: found.line };
}
