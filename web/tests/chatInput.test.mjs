import assert from "node:assert/strict";
import test from "node:test";

import {
  assembleChatTurn,
  chatAnnotationComposerPosition,
  replaceTextSpan,
} from "../src/chatInput.ts";

test("dictation inserts at the captured cursor and revises only its active span", () => {
  const first = replaceTextSpan("before  after", { start: 7, end: 7 }, "partial");
  assert.deepEqual(first, { value: "before partial after", end: 14 });

  const revised = replaceTextSpan(first.value, { start: 7, end: first.end }, "final words");
  assert.deepEqual(revised, { value: "before final words after", end: 18 });
});

test("chat annotations become plain selected text and comments in the outgoing turn", () => {
  const annotations = [
    { selectedText: "First answer sentence.", comment: "Be more specific." },
    { selectedText: "Second answer sentence.", comment: "  Is this measured?  " },
  ];

  assert.equal(
    assembleChatTurn("Check both points.", annotations),
    [
      "Check both points.",
      "First answer sentence.\ncomment: Be more specific.",
      "Second answer sentence.\ncomment: Is this measured?",
    ].join("\n\n"),
  );
  assert.equal(
    assembleChatTurn("", annotations.slice(0, 1)),
    "First answer sentence.\ncomment: Be more specific.",
  );
});

test("annotation composer stays beside the selection and inside the viewport", () => {
  assert.deepEqual(
    chatAnnotationComposerPosition(
      { left: 100, right: 180, top: 140 },
      { width: 1000, height: 800 },
    ),
    { left: 190, top: 140 },
  );
  assert.deepEqual(
    chatAnnotationComposerPosition(
      { left: 700, right: 790, top: 760 },
      { width: 800, height: 800 },
    ),
    { left: 370, top: 560 },
  );
  assert.deepEqual(
    chatAnnotationComposerPosition({ left: 4, right: 796, top: -10 }, { width: 800, height: 800 }),
    { left: 12, top: 12 },
  );
});
