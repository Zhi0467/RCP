/** A finished Paper turn as the server lists it, and one file's diff as rows. */

export interface PaperTurnRow {
  path: string;
  kind: "write" | "delete" | "rename" | "outside_turn" | "unsupported";
  clash: boolean;
  added: number | null;
  removed: number | null;
}

export interface PaperTurn {
  before_version_id: string;
  after_version_id: string;
  files: PaperTurnRow[];
}

export interface PaperTurnHunk {
  old_start: number;
  new_start: number;
  old: string[];
  new: string[];
}

export interface PaperTurnFile {
  path: string;
  kind: PaperTurnRow["kind"];
  clash: boolean;
  before: string | null;
  coach: string | null;
  current: string | null;
  hunks: PaperTurnHunk[];
}

export type DiffRow =
  | { kind: "same" | "added" | "removed"; line: number; text: string }
  | { kind: "gap"; hidden: number };

/** Unchanged lines kept on each side of an edit. */
export const DIFF_CONTEXT = 3;

export function textLines(text: string): string[] {
  if (!text) return [];
  return (text.endsWith("\n") ? text.slice(0, -1) : text).split("\n");
}

export function turnTotals(rows: readonly PaperTurnRow[]): { added: number; removed: number } {
  return rows.reduce(
    (total, row) => ({
      added: total.added + (row.added ?? 0),
      removed: total.removed + (row.removed ?? 0),
    }),
    { added: 0, removed: 0 },
  );
}

/** The agent's edits against the turn's start, numbered as the side each line lives on;
 *  unchanged runs keep `DIFF_CONTEXT` lines beside each edit and fold the rest. */
export function diffRows(file: PaperTurnFile, context = DIFF_CONTEXT): DiffRow[] {
  const before = textLines(file.before ?? "");
  const rows: DiffRow[] = [];
  let old = 1;
  let shift = 0;
  const same = (from: number, to: number, last: boolean) => {
    const run = to - from;
    if (run <= 0) return;
    const head = rows.length ? Math.min(context, run) : 0;
    const tail = last ? 0 : Math.min(context, run - head);
    const push = (line: number) =>
      rows.push({ kind: "same", line: line + shift, text: before[line - 1] });
    for (let line = from; line < from + head; line++) push(line);
    if (run > head + tail) rows.push({ kind: "gap", hidden: run - head - tail });
    for (let line = to - tail; line < to; line++) push(line);
  };
  for (const hunk of file.hunks) {
    same(old, hunk.old_start, false);
    hunk.old.forEach((text, index) =>
      rows.push({ kind: "removed", line: hunk.old_start + index, text }),
    );
    hunk.new.forEach((text, index) =>
      rows.push({ kind: "added", line: hunk.new_start + index, text }),
    );
    old = hunk.old_start + hunk.old.length;
    shift = hunk.new_start + hunk.new.length - old;
  }
  same(old, before.length + 1, true);
  return rows;
}

/** What a comment on a diff line names: the side and number the line has there. */
export function diffLineLabel(row: Exclude<DiffRow, { kind: "gap" }>): string {
  return `${row.kind === "removed" ? "old" : "new"} line ${row.line}`;
}
