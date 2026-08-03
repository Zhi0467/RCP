import assert from "node:assert/strict";
import test from "node:test";

import {
  clampFloatingPosition,
  clampFloatingSize,
  defaultFloatingPosition,
  floatingWindowSize,
  movedPosition,
  nodeDetailSizeStorageKey,
  parseFloatingSize,
  resizedFloatingSize,
} from "../src/floatingWindow.ts";

test("floating windows clamp to reachable viewport positions", () => {
  const size = { width: 400, height: 300 };
  const viewport = { width: 1000, height: 700 };
  assert.deepEqual(clampFloatingPosition({ x: -200, y: -4 }, size, viewport), { x: 12, y: 12 });
  assert.deepEqual(clampFloatingPosition({ x: 900, y: 600 }, size, viewport), { x: 588, y: 388 });
});

test("resizable windows respect their minimum and the live viewport", () => {
  const minimum = { width: 360, height: 320 };
  assert.deepEqual(
    clampFloatingSize({ width: 200, height: 100 }, { width: 1000, height: 700 }, minimum),
    { width: 360, height: 320 },
  );
  assert.deepEqual(
    clampFloatingSize({ width: 1200, height: 900 }, { width: 1000, height: 700 }, minimum),
    { width: 976, height: 676 },
  );
  assert.deepEqual(resizedFloatingSize({ width: 590, height: 720 }, { x: 48, y: -24 }), {
    width: 638,
    height: 696,
  });
});

test("node detail sizes have a project key and reject corrupt stored values", () => {
  assert.equal(nodeDetailSizeStorageKey("project-a"), "rcp:node-detail-size:project-a");
  assert.deepEqual(parseFloatingSize('{"width":640,"height":520}'), {
    width: 640,
    height: 520,
  });
  assert.equal(parseFloatingSize('{"width":0,"height":520}'), null);
  assert.equal(parseFloatingSize("not json"), null);
});

test("detail and chat use independent non-overlapping wide-screen defaults", () => {
  const viewport = { width: 1440, height: 900 };
  assert.deepEqual(defaultFloatingPosition("chat", viewport), { x: 12, y: 118 });
  assert.deepEqual(defaultFloatingPosition("detail", viewport), { x: 838, y: 118 });
  assert.deepEqual(movedPosition({ x: 20, y: 30 }, { x: 100, y: 100 }, { x: 145, y: 80 }), {
    x: 65,
    y: 10,
  });
});

test("supported compact windows start below navigation and side by side", () => {
  const viewport = { width: 880, height: 600 };
  const chatSize = floatingWindowSize("chat", viewport);
  const detailSize = floatingWindowSize("detail", viewport);
  const chat = defaultFloatingPosition("chat", viewport);
  const detail = defaultFloatingPosition("detail", viewport);
  assert.deepEqual(chatSize, { width: 422, height: 470 });
  assert.deepEqual(detailSize, { width: 422, height: 470 });
  assert.equal(chat.y, 118);
  assert.equal(detail.y, 118);
  assert.ok(chat.x + chatSize.width < detail.x);
  assert.ok(detail.x + detailSize.width <= viewport.width - 12);
});
