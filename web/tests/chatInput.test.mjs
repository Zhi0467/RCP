import assert from "node:assert/strict";
import test from "node:test";

import {
  assembleChatTurn,
  chatAnnotationComposerPosition,
  chatSelectionCommentPosition,
  parseStagedChatAnnotations,
  replaceTextSpan,
  stagedArtifactContext,
} from "../src/chat/chatInput.ts";

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

  for (const [draft, selected] of [
    ["Check both points.", annotations],
    ["", annotations.slice(0, 1)],
  ]) {
    const result = assembleChatTurn(draft, selected);
    if (draft) assert.ok(result.includes(draft));
    for (const annotation of selected) {
      assert.ok(result.includes(annotation.selectedText));
      assert.ok(result.includes(annotation.comment.trim()));
    }
  }
});

test("artifact comments are chips like answer comments, numbered as their selections", () => {
  const context = { source: "task", operation_id: "operation", artifact_id: "artifact" };
  const box = { kind: "box", rect: { x: 0, y: 0, width: 1, height: 1 }, comment: "" };
  const chips = [
    {
      id: "a",
      selectedText: "boxed plot",
      comment: "Why flat?",
      artifact: { context, name: "r.html", selection: box },
    },
    { id: "b", selectedText: "An answer sentence.", comment: "Source?" },
    {
      id: "c",
      selectedText: '"the spike"',
      comment: "Cause?",
      artifact: { context, name: "r.html", selection: { ...box, kind: "text" } },
    },
  ];
  // A chip survives the tab's draft storage with its artifact target.
  const staged = parseStagedChatAnnotations(JSON.stringify(chips));
  assert.deepEqual(staged, chips);

  // Only answer comments are written into the text; artifact comments ride the selections.
  const turn = assembleChatTurn("", staged);
  assert.ok(turn.includes("An answer sentence.\ncomment: Source?"));
  assert.ok(!turn.includes("Why flat?") && !turn.includes("Cause?"));

  // The turn carries the artifact selections in the order they are numbered, with
  // the comment as edited on the chip.
  assert.deepEqual(stagedArtifactContext(staged), {
    ...context,
    selections: [
      { ...box, comment: "Why flat?" },
      { ...box, kind: "text", comment: "Cause?" },
    ],
  });
  assert.equal(stagedArtifactContext(staged.filter((chip) => !chip.artifact)), null);
});

test("annotation composer stays beside the selection and inside the viewport", () => {
  assert.deepEqual(
    chatAnnotationComposerPosition(
      { left: 100, right: 180, top: 140 },
      { left: 0, top: 0, width: 1000, height: 800 },
      { width: 320, height: 228 },
    ),
    { left: 190, top: 140 },
  );
  assert.deepEqual(
    chatAnnotationComposerPosition(
      { left: 700, right: 790, top: 760 },
      { left: 0, top: 0, width: 800, height: 800 },
      { width: 320, height: 228 },
    ),
    { left: 370, top: 560 },
  );
  assert.deepEqual(
    chatAnnotationComposerPosition(
      { left: 4, right: 796, top: -10 },
      { left: 0, top: 0, width: 800, height: 800 },
      { width: 320, height: 228 },
    ),
    { left: 12, top: 12 },
  );

  assert.deepEqual(
    chatAnnotationComposerPosition(
      { left: 720, right: 760, top: 400 },
      { left: 0, top: 48, width: 844, height: 196 },
      { width: 320, height: 172 },
    ),
    { left: 390, top: 60 },
  );
});

test("the selection Comment offer sits below the selection and stays on screen", () => {
  const viewport = { left: 0, top: 0, width: 390, height: 844 };
  const button = { width: 112, height: 44 };
  assert.deepEqual(
    chatSelectionCommentPosition({ left: 40, right: 300, top: 400, bottom: 420 }, viewport, button),
    { left: 188, top: 438 },
  );
  // No room below: flip above the selection.
  assert.deepEqual(
    chatSelectionCommentPosition({ left: 40, right: 300, top: 790, bottom: 810 }, viewport, button),
    { left: 188, top: 728 },
  );
  // A selection ending at either edge keeps the whole button inside the margins.
  assert.equal(
    chatSelectionCommentPosition({ left: 0, right: 30, top: 400, bottom: 420 }, viewport, button)
      .left,
    12,
  );
  assert.equal(
    chatSelectionCommentPosition({ left: 0, right: 389, top: 400, bottom: 420 }, viewport, button)
      .left,
    266,
  );
});
