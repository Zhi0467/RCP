import assert from "node:assert/strict";
import { test } from "node:test";

import { arrowScrollsTranscript } from "../src/ui/composerKeys.ts";

function keyEvent(key, value = "", extra = {}) {
  const event = {
    key,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    defaultPrevented: false,
    nativeEvent: { isComposing: false },
    currentTarget: { value },
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
    ...extra,
  };
  return event;
}

function scroller() {
  return {
    moved: 0,
    scrollBy({ top }) {
      this.moved += top;
    },
  };
}

test("Up and Down in an empty composer scroll the transcript", () => {
  const lines = scroller();
  const up = keyEvent("ArrowUp");
  assert.equal(arrowScrollsTranscript(up, lines), true);
  assert.equal(up.prevented, true);
  assert.ok(lines.moved < 0);
  const down = keyEvent("ArrowDown");
  assert.equal(arrowScrollsTranscript(down, lines), true);
  assert.equal(lines.moved, 0);
});

test("arrows keep moving the caret when the composer has text, a modifier, a menu or an IME", () => {
  for (const event of [
    keyEvent("ArrowUp", "draft"),
    keyEvent("ArrowDown", "", { shiftKey: true }),
    keyEvent("ArrowUp", "", { metaKey: true }),
    keyEvent("ArrowUp", "", { defaultPrevented: true }),
    keyEvent("ArrowDown", "", { nativeEvent: { isComposing: true } }),
    keyEvent("ArrowLeft"),
  ]) {
    const lines = scroller();
    assert.equal(arrowScrollsTranscript(event, lines), false);
    assert.equal(event.prevented, false);
    assert.equal(lines.moved, 0);
  }
  assert.equal(arrowScrollsTranscript(keyEvent("ArrowUp"), null), false);
});
