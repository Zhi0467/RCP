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

const wideGraph = [
  { left: 0, top: 0, width: 200, height: 100 },
  { left: 1400, top: 900, width: 200, height: 100 },
];

test("fit frames a graph that overflows the pane and never magnifies a small one", () => {
  const fitted = fitDagToViewport({
    nodes: wideGraph,
    viewportWidth: 800,
    viewportHeight: 500,
  });
  assert.ok(fitted);
  assert.ok(fitted.zoom < 1, "an oversized graph zooms out to fit");
  // Every node lands inside the pane once the fit is applied.
  for (const node of wideGraph) {
    assert.ok(node.left * fitted.zoom - fitted.scrollLeft >= -1e-6);
    assert.ok((node.left + node.width) * fitted.zoom - fitted.scrollLeft <= 800 + 1e-6);
    assert.ok(node.top * fitted.zoom - fitted.scrollTop >= -1e-6);
    assert.ok((node.top + node.height) * fitted.zoom - fitted.scrollTop <= 500 + 1e-6);
  }

  const small = fitDagToViewport({
    nodes: [{ left: 10, top: 10, width: 120, height: 60 }],
    viewportWidth: 1200,
    viewportHeight: 800,
  });
  assert.equal(small.zoom, 1, "a graph that already fits keeps its authored size");
});

test("fit reaches below the pinch floor so a large graph actually fits", () => {
  // The graph that motivated framing: ~1680x2090px of nodes in a ~475px pane.
  const tall = fitDagToViewport({
    nodes: [
      { left: 0, top: 0, width: 10, height: 10 },
      { left: 1670, top: 2080, width: 10, height: 10 },
    ],
    viewportWidth: 900,
    viewportHeight: 475,
  });

  assert.ok(tall.zoom < DAG_ZOOM_MIN, "the pinch floor cannot frame this graph");
  assert.ok(2090 * tall.zoom <= 475, "every node fits the pane at the fitted scale");
  assert.ok(1680 * tall.zoom <= 900);
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
