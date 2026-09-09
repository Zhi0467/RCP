import assert from "node:assert/strict";
import test from "node:test";

import {
  DAG_ZOOM_MAX,
  DAG_FIT_ZOOM_MIN,
  DAG_ZOOM_MIN,
  fitDagToViewport,
  zoomDagAtPoint,
} from "../src/hooks/dagZoom.ts";

const base = {
  zoom: 1,
  focalX: 320,
  focalY: 240,
  scrollLeft: 700,
  scrollTop: 400,
};

test("pinch delta zooms in and out in the expected direction", () => {
  assert.ok(zoomDagAtPoint({ ...base, deltaY: -80 }).zoom > base.zoom);
  assert.ok(zoomDagAtPoint({ ...base, deltaY: 80 }).zoom < base.zoom);
});

test("pinch zoom clamps at usable endpoints", () => {
  assert.equal(zoomDagAtPoint({ ...base, deltaY: -100_000 }).zoom, DAG_ZOOM_MAX);
  assert.equal(zoomDagAtPoint({ ...base, deltaY: 100_000 }).zoom, DAG_ZOOM_MIN);
});

test("pinch zoom preserves the graph point beneath the focal point", () => {
  const beforeX = (base.scrollLeft + base.focalX) / base.zoom;
  const beforeY = (base.scrollTop + base.focalY) / base.zoom;
  const result = zoomDagAtPoint({ ...base, deltaY: -120 });

  assert.ok(Math.abs((result.scrollLeft + base.focalX) / result.zoom - beforeX) < 1e-9);
  assert.ok(Math.abs((result.scrollTop + base.focalY) / result.zoom - beforeY) < 1e-9);
});

test("pinch keeps the focal graph point after Fit adds canvas space", () => {
  const centered = { ...base, offsetX: 430, offsetY: 24 };
  const beforeX = (centered.scrollLeft + centered.focalX - centered.offsetX) / centered.zoom;
  const beforeY = (centered.scrollTop + centered.focalY - centered.offsetY) / centered.zoom;
  const result = zoomDagAtPoint({ ...centered, deltaY: -120 });
  assert.ok(
    Math.abs((result.scrollLeft + centered.focalX - centered.offsetX) / result.zoom - beforeX) <
      1e-9,
  );
  assert.ok(
    Math.abs((result.scrollTop + centered.focalY - centered.offsetY) / result.zoom - beforeY) <
      1e-9,
  );
});

const wideGraph = [
  { left: 0, top: 0, width: 200, height: 100 },
  { left: 1400, top: 900, width: 200, height: 100 },
];

test("fit fills the width, leaves tall graphs scrollable, and never magnifies a small one", () => {
  const fitted = fitDagToViewport({
    nodes: wideGraph,
    viewportWidth: 800,
    viewportHeight: 500,
  });
  assert.ok(fitted);
  assert.ok(fitted.zoom < 1, "an oversized graph zooms out to fit");
  // Every column lands inside the pane, even when the graph is taller than it.
  for (const node of wideGraph) {
    assert.ok(node.left * fitted.zoom + fitted.offsetX - fitted.scrollLeft >= 32 - 1e-6);
    assert.ok(
      (node.left + node.width) * fitted.zoom + fitted.offsetX - fitted.scrollLeft <= 768 + 1e-6,
    );
  }
  assert.equal(fitted.offsetY - fitted.scrollTop, 32);

  const small = fitDagToViewport({
    nodes: [{ left: 10, top: 10, width: 120, height: 60 }],
    viewportWidth: 1200,
    viewportHeight: 800,
  });
  assert.equal(small.floor, DAG_ZOOM_MIN);
  assert.equal(small.zoom, 1, "a graph that already fits keeps its authored size");
});

test("fit reaches below the pinch floor when columns exceed the pane width", () => {
  const tall = fitDagToViewport({
    nodes: [
      { left: 0, top: 0, width: 10, height: 10 },
      { left: 1670, top: 2080, width: 10, height: 10 },
    ],
    viewportWidth: 900,
    viewportHeight: 475,
  });

  assert.equal(tall.floor, tall.zoom);
  assert.ok(tall.zoom < DAG_ZOOM_MIN, "the pinch floor cannot frame this graph");
  assert.ok(2090 * tall.zoom > 475, "height does not force all rows into the pane");
  assert.equal(1680 * tall.zoom, 900 - 64);
});

test("Fit centers columns near the origin and starts tall graphs at the top", () => {
  for (const [contentWidth, contentHeight] of [
    [1600, 2400],
    [2400, 300],
  ]) {
    const fitted = fitDagToViewport({
      nodes: [{ left: 54, top: 56, width: contentWidth, height: contentHeight }],
      viewportWidth: 1500,
      viewportHeight: 650,
    });
    const left = 54 * fitted.zoom + fitted.offsetX - fitted.scrollLeft;
    const top = 56 * fitted.zoom + fitted.offsetY - fitted.scrollTop;
    assert.ok(left >= 32 - 1e-9 && top >= 32 - 1e-9);
    assert.ok(Math.abs(left + (contentWidth * fitted.zoom) / 2 - 750) < 1e-9);
    if (contentHeight * fitted.zoom > 650 - 64) {
      assert.ok(Math.abs(top - 32) < 1e-9);
    } else {
      assert.ok(Math.abs(top + (contentHeight * fitted.zoom) / 2 - 325) < 1e-9);
    }
  }
});

test("adding rows never shrinks the fitted columns", () => {
  const fit = (height) =>
    fitDagToViewport({
      nodes: [{ left: 54, top: 56, width: 2000, height }],
      viewportWidth: 1500,
      viewportHeight: 650,
    });
  assert.equal(fit(100).zoom, fit(3000).zoom);
});

test("fit still has a floor rather than collapsing to nothing", () => {
  const fitted = fitDagToViewport({
    nodes: [
      { left: 0, top: 0, width: 10, height: 10 },
      { left: 100_000, top: 100_000, width: 10, height: 10 },
    ],
    viewportWidth: 800,
    viewportHeight: 500,
  });

  assert.equal(fitted.zoom, DAG_FIT_ZOOM_MIN);
  assert.equal(fitted.floor, DAG_FIT_ZOOM_MIN);
});

test("fit reports nothing to frame instead of guessing", () => {
  assert.equal(fitDagToViewport({ nodes: [], viewportWidth: 800, viewportHeight: 500 }), null);
  assert.equal(fitDagToViewport({ nodes: wideGraph, viewportWidth: 0, viewportHeight: 500 }), null);
});

test("a gesture never reverses the direction it was asked for", () => {
  // Fit can leave the canvas below the pinch floor. A zoom-out from there used
  // to be clamped back up to DAG_ZOOM_MIN, jumping inward instead of outward.
  const fitted = 0.2;
  const base = { zoom: fitted, focalX: 400, focalY: 250, scrollLeft: 0, scrollTop: 0 };

  const out = zoomDagAtPoint({ ...base, deltaY: 200 });
  assert.ok(out.zoom <= fitted, `zooming out went inward to ${out.zoom}`);

  const inward = zoomDagAtPoint({ ...base, deltaY: -200 });
  assert.ok(inward.zoom > fitted, "zooming in still zooms in from a fitted view");
  assert.ok(inward.zoom <= DAG_ZOOM_MAX);
});

test("the pinch floor is unchanged for an ordinary view", () => {
  const base = { zoom: 1, focalX: 400, focalY: 250, scrollLeft: 0, scrollTop: 0 };
  assert.equal(zoomDagAtPoint({ ...base, deltaY: 100_000 }).zoom, DAG_ZOOM_MIN);
});

test("a fitted view stays reachable after zooming in and back out", () => {
  // The floor is the caller's stable fitted scale. Deriving it from the live
  // zoom instead would promote each intermediate scale to the new floor, so
  // sub-0.5 zoom would be one-way and Fit unreachable without pressing it again.
  const fitted = 0.2;
  const base = { focalX: 400, focalY: 250, scrollLeft: 0, scrollTop: 0, minZoom: fitted };

  const inward = zoomDagAtPoint({ ...base, zoom: fitted, deltaY: -200 });
  assert.ok(inward.zoom > fitted, "zooming in leaves the fitted scale");

  const back = zoomDagAtPoint({ ...base, zoom: inward.zoom, deltaY: 100_000 });
  assert.equal(back.zoom, fitted, "zooming out returns to the fitted scale");
});

test("a remembered viewport carries the fitted floor independently of its current zoom", () => {
  const fitted = fitDagToViewport({
    nodes: wideGraph,
    viewportWidth: 800,
    viewportHeight: 500,
  });
  assert.ok(fitted.floor < DAG_ZOOM_MIN);
  const inward = zoomDagAtPoint({
    ...base,
    ...fitted,
    minZoom: fitted.floor,
    deltaY: -500,
  });
  assert.ok(inward.zoom > DAG_ZOOM_MIN);
  const remembered = { ...inward, floor: fitted.floor };
  const restored = { ...remembered };
  const back = zoomDagAtPoint({
    ...base,
    ...restored,
    minZoom: restored.floor,
    deltaY: 100_000,
  });
  assert.equal(back.zoom, fitted.zoom);
});
