import assert from "node:assert/strict";
import test from "node:test";

import { chatSelectionCommentPosition, replaceTextSpan } from "../src/chat/chatInput.ts";

test("dictation inserts at the captured cursor and revises only its active span", () => {
  const first = replaceTextSpan("before  after", { start: 7, end: 7 }, "partial");
  assert.deepEqual(first, { value: "before partial after", end: 14 });

  const revised = replaceTextSpan(first.value, { start: 7, end: first.end }, "final words");
  assert.deepEqual(revised, { value: "before final words after", end: 18 });
});

test("the selection Comment offer sits on top of the selection and stays on screen", () => {
  const viewport = { left: 0, top: 0, width: 390, height: 844 };
  const button = { width: 112, height: 44 };
  const at = (left, right, top, bottom) =>
    chatSelectionCommentPosition({ firstLine: { left, right, top }, bottom }, viewport, button);
  // Centred above the first line of a two-line selection.
  assert.deepEqual(at(40, 300, 400, 460), { left: 114, top: 350 });
  // No room above: below the selection's last line instead.
  assert.deepEqual(at(40, 300, 30, 70), { left: 114, top: 76 });
  // A selection at either edge keeps the whole button inside the margins.
  assert.equal(at(0, 30, 400, 420).left, 12);
  assert.equal(at(300, 389, 400, 420).left, 266);
});
