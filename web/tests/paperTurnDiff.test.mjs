import assert from "node:assert/strict";
import { test } from "node:test";
import { diffLineLabel, diffRows, turnTotals } from "../src/paper/turnDiff.ts";

const lines = (count) =>
  Array.from({ length: count }, (_, index) => `line ${index + 1}`).join("\n") + "\n";

test("a one-line edit shows its context, folds the rest, and numbers each side", () => {
  const rows = diffRows({
    before: lines(20),
    hunks: [{ old_start: 10, new_start: 10, old: ["line 10"], new: ["changed", "added"] }],
  });
  assert.deepEqual(
    rows.map((row) => (row.kind === "gap" ? `gap ${row.hidden}` : `${row.kind} ${row.line}`)),
    [
      "gap 6",
      "same 7",
      "same 8",
      "same 9",
      "removed 10",
      "added 10",
      "added 11",
      "same 12",
      "same 13",
      "same 14",
      "gap 7",
    ],
  );
  assert.equal(rows[4].text, "line 10");
  assert.equal(rows[7].text, "line 11");
  assert.equal(diffLineLabel(rows[4]), "old line 10");
  assert.equal(diffLineLabel(rows[6]), "new line 11");
});

test("an addition at the end of a short file is one added line", () => {
  const rows = diffRows({
    before: "b\n",
    hunks: [{ old_start: 2, new_start: 2, old: [], new: ["agent"] }],
  });
  assert.deepEqual(
    rows.map((row) => [row.kind, row.line, row.text]),
    [
      ["same", 1, "b"],
      ["added", 2, "agent"],
    ],
  );
});

test("totals add the counted rows and skip files without lines", () => {
  assert.deepEqual(
    turnTotals([
      { added: 3, removed: 1 },
      { added: null, removed: null },
      { added: 0, removed: 4 },
    ]),
    { added: 3, removed: 5 },
  );
});
