import assert from "node:assert/strict";
import test from "node:test";

import { chatAnnotationViewportMetrics } from "../src/chat/chatInput.ts";

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
