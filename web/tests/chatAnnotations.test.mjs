import assert from "node:assert/strict";
import test from "node:test";

import {
  chatAnnotationTextControlSelection,
  chatAnnotationViewportMetrics,
  parseStagedChatAnnotations,
  stagedChatAnnotationsAreComplete,
} from "../src/chatInput.ts";

test("the read-only selection control reports exactly the selected text", () => {
  const value = "The baseline improved by 12%, but variance was not reported.";
  const selectedText = "variance was not reported";
  const selectionStart = value.indexOf(selectedText);
  const control = {
    value,
    selectionStart,
    selectionEnd: selectionStart + selectedText.length,
  };
  assert.equal(chatAnnotationTextControlSelection(control), selectedText);
  assert.equal(
    chatAnnotationTextControlSelection({ ...control, selectionEnd: selectionStart }),
    "",
  );
});

test("a comment edited blank survives a switch away and back and still blocks send", () => {
  const persisted = JSON.stringify([
    {
      id: "annotation-1",
      selectedText: "The reported result.",
      comment: "",
    },
  ]);

  const restoredAfterChatSwitch = parseStagedChatAnnotations(persisted);

  assert.deepEqual(restoredAfterChatSwitch, [
    {
      id: "annotation-1",
      selectedText: "The reported result.",
      comment: "",
    },
  ]);
  assert.equal(stagedChatAnnotationsAreComplete(restoredAfterChatSwitch), false);
});

test("the composer follows the soft-keyboard viewport at every layout width", () => {
  assert.deepEqual(
    chatAnnotationViewportMetrics(
      { width: 844, height: 520 },
      { width: 844, height: 196, offsetLeft: 0, offsetTop: 48 },
    ),
    {
      left: 0,
      top: 48,
      width: 844,
      height: 196,
      right: 0,
      bottom: 276,
    },
  );
  assert.deepEqual(chatAnnotationViewportMetrics({ width: 800, height: 800 }), {
    left: 0,
    top: 0,
    width: 800,
    height: 800,
    right: 0,
    bottom: 0,
  });
});
