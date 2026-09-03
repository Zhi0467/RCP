import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  chatAnnotationTextControlSelection,
  chatAnnotationViewportMetrics,
  parseStagedChatAnnotations,
  stagedChatAnnotationsAreComplete,
} from "../src/chatInput.ts";

const nodeChatSource = await readFile(
  new URL("../src/components/NodeChat.tsx", import.meta.url),
  "utf8",
);
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

test("assistant answers expose pointer selection and a real keyboard selection command", () => {
  assert.match(nodeChatSource, /className="chat-markdown chat-annotatable-answer"/);
  assert.match(nodeChatSource, /onPointerUp=/);
  assert.doesNotMatch(
    nodeChatSource,
    /className="chat-markdown chat-annotatable-answer"\s+tabIndex=/,
  );
  assert.match(nodeChatSource, /aria-label="Comment on this answer"/);
  assert.match(nodeChatSource, /aria-label="Select answer text"/);
  assert.match(nodeChatSource, /className="chat-annotation-source"/);
  const selectionControl = nodeChatSource.slice(
    nodeChatSource.indexOf('className="chat-annotation-source"'),
    nodeChatSource.indexOf("/>", nodeChatSource.indexOf('className="chat-annotation-source"')),
  );
  assert.match(selectionControl, /\breadOnly\b/);
  assert.match(selectionControl, /onSelect=/);
  assert.doesNotMatch(
    selectionControl,
    /aria-readonly|onBeforeInput=|onCut=|onDrop=|onPaste=|onChange=/,
  );
  assert.match(nodeChatSource, /createPortal\(/);
  assert.match(nodeChatSource, /event\.key === "Escape"/);
  assert.match(nodeChatSource, /event\.metaKey \|\| event\.ctrlKey/);
  assert.match(styles, /\.chat-annotation-composer\s*\{[\s\S]*?position: fixed/);

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

test("staged annotations can be counted, edited, and removed before the ordinary send", () => {
  assert.match(nodeChatSource, /annotations\.length} annotation/);
  assert.match(nodeChatSource, /Comment for annotation/);
  assert.match(nodeChatSource, /updateAnnotation\(annotation\.id/);
  assert.match(nodeChatSource, /Remove annotation/);

  const send = nodeChatSource.slice(
    nodeChatSource.indexOf("const send = async"),
    nodeChatSource.indexOf("const repairGraphUpdate"),
  );
  assert.match(send, /assembleChatTurn\(message, annotations\)/);
  assert.match(send, /message: text/);
  assert.doesNotMatch(send, /annotation_context|message_id|source_id|offset/);
  assert.ok(send.indexOf("await onStartTask") < send.indexOf("setAnnotations([])"));
  assert.match(send, /setMessage\(\(current\) => \(current \? current : draftMessage\)\)/);
});

test("annotation creation and editing are fenced while a turn is submitting", () => {
  assert.match(
    nodeChatSource,
    /if \(submitting\) return;[\s\S]*?const selection = window\.getSelection/,
  );
  assert.match(
    nodeChatSource,
    /aria-label="Comment on this answer"[\s\S]*?disabled=\{submitting\}/,
  );
  assert.match(
    nodeChatSource,
    /aria-label=\{`Comment for annotation \$\{index \+ 1\}`\}[\s\S]*?disabled=\{submitting\}/,
  );
  assert.match(
    nodeChatSource,
    /aria-label=\{`Remove annotation \$\{index \+ 1\}`\}[\s\S]*?disabled=\{submitting\}/,
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
  assert.match(nodeChatSource, /!annotationsComplete \|\|/);
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
  assert.match(nodeChatSource, /window\.visualViewport/);
  assert.match(nodeChatSource, /getBoundingClientRect\(\)/);
  assert.match(nodeChatSource, /new ResizeObserver\(update\)/);
  assert.match(
    styles,
    /\.chat-annotation-composer\s*\{[\s\S]*?--chat-annotation-viewport-height[\s\S]*?overflow-y: auto[\s\S]*?overscroll-behavior: contain/,
  );
});
