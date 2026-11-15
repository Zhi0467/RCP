import assert from "node:assert/strict";
import test from "node:test";
import { createFrameLocator } from "../src/comments/viewerFrame.ts";

const anchor = { kind: "text", text: "quote", comment: "" };
function fixture() {
  const sent = [];
  const source = { postMessage: (message) => sent.push(message) };
  const frame = {
    contentWindow: source,
    getBoundingClientRect: () => ({ x: 20, y: 30, width: 400, height: 300 }),
  };
  let changes = 0;
  const locator = createFrameLocator(frame, () => changes++);
  const message = (rect, overrides = {}) =>
    locator.receive({
      source,
      data: { type: "rcp-artifact-position", version: 1, id: "t", rect },
      ...overrides,
    });
  return { locator, sent, message, changes: () => changes };
}
test("frame pins use shell coordinates and refresh without requesting a scroll", () => {
  const { locator, sent, message } = fixture();
  locator.track([{ thread_id: "t", anchor }]);
  assert.equal(sent[0].scroll, false);
  message({ x: 10, y: 20, width: 80, height: 10 });
  assert.deepEqual(locator.position("t"), { x: 110, y: 60 });
  message({ x: 10, y: -5, width: 80, height: 10 });
  assert.deepEqual(locator.position("t"), { x: 110, y: 35 });
  locator.locate({ thread_id: "t", anchor });
  assert.equal(sent.at(-1).scroll, true);
});
test("missing, offscreen and untrusted frame positions cannot create pins", () => {
  const { locator, message, changes } = fixture();
  locator.track([{ thread_id: "t", anchor }]);
  message({ x: 1, y: 2, width: 3, height: 4 }, { source: {} });
  message({ x: NaN, y: 2, width: 3, height: 4 });
  assert.equal(changes(), 0);
  assert.equal(locator.position("t"), null);
  message({ x: 10, y: -100, width: 80, height: 10 });
  assert.equal(locator.position("t"), null);
  message(null);
  assert.equal(locator.position("t"), null);
});
test("removed threads are untracked and frame reload re-requests retained anchors", () => {
  const { locator, sent } = fixture();
  locator.track([{ thread_id: "t", anchor }]);
  locator.reload();
  assert.equal(sent.length, 2);
  locator.track([]);
  assert.equal(sent.at(-1).anchor, null);
});
