export const DAG_ZOOM_MIN = 0.5;
/** Fit's own floor, allowing columns wider than the pane to fit. */
export const DAG_FIT_ZOOM_MIN = 0.05;
export const DAG_ZOOM_MAX = 2.5;

export interface DagZoomResult {
  zoom: number;
  scrollLeft: number;
  scrollTop: number;
}

/** Where the DAG was last looked at, so leaving and returning restores the view. */
export interface DagViewport extends DagZoomResult {
  floor?: number;
  /** Pixel space before the scaled canvas, allowing Fit to center near-origin nodes. */
  offsetX?: number;
  offsetY?: number;
}

interface DagZoomInput extends DagZoomResult {
  deltaY: number;
  focalX: number;
  focalY: number;
  /** How far out a gesture may go, from the caller's stable floor.
   *
   * Fit can leave the canvas below DAG_ZOOM_MIN. Deriving the floor from the
   * live zoom instead would make every intermediate scale the new floor, so
   * zooming in from a fitted view would ratchet and never return to it.
   */
  minZoom?: number;
  offsetX?: number;
  offsetY?: number;
}

export function zoomDagAtPoint({
  zoom,
  deltaY,
  focalX,
  focalY,
  scrollLeft,
  scrollTop,
  minZoom = DAG_ZOOM_MIN,
  offsetX = 0,
  offsetY = 0,
}: DagZoomInput): DagZoomResult {
  const nextZoom = clamp(zoom * Math.exp(-deltaY * 0.002), Math.min(minZoom, zoom), DAG_ZOOM_MAX);
  const ratio = nextZoom / zoom;
  return {
    zoom: nextZoom,
    scrollLeft: (scrollLeft + focalX - offsetX) * ratio - focalX + offsetX,
    scrollTop: (scrollTop + focalY - offsetY) * ratio - focalY + offsetY,
  };
}

/** One laid-out node box in unscaled canvas coordinates. */
export interface DagNodeBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

interface DagFitInput {
  nodes: DagNodeBox[];
  viewportWidth: number;
  viewportHeight: number;
  padding?: number;
}

/** Fit the graph's width and leave tall columns vertically scrollable.
 *
 * Returns null when there is nothing to frame yet. The result never magnifies
 * past 1: a small graph keeps its authored node size instead of ballooning to
 * fill the pane.
 *
 * Height does not reduce the scale: long columns start at the top of the pane;
 * a short graph is centered vertically.
 */
export function fitDagToViewport({
  nodes,
  viewportWidth,
  viewportHeight,
  padding = 32,
}: DagFitInput): DagViewport | null {
  if (nodes.length === 0 || viewportWidth <= 0 || viewportHeight <= 0) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const node of nodes) {
    minX = Math.min(minX, node.left);
    minY = Math.min(minY, node.top);
    maxX = Math.max(maxX, node.left + node.width);
    maxY = Math.max(maxY, node.top + node.height);
  }
  const contentWidth = maxX - minX;
  const contentHeight = maxY - minY;
  if (!(contentWidth > 0) || !(contentHeight > 0)) return null;
  const usableWidth = Math.max(1, viewportWidth - padding * 2);
  const zoom = clamp(usableWidth / contentWidth, DAG_FIT_ZOOM_MIN, 1);
  const left = minX * zoom - (viewportWidth - contentWidth * zoom) / 2;
  const top = minY * zoom - Math.max(padding, (viewportHeight - contentHeight * zoom) / 2);
  return {
    zoom,
    floor: Math.min(DAG_ZOOM_MIN, zoom),
    offsetX: Math.max(0, -left),
    offsetY: Math.max(0, -top),
    scrollLeft: Math.max(0, left),
    scrollTop: Math.max(0, top),
  };
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}
