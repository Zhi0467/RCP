export const DAG_ZOOM_MIN = 0.5;
export const DAG_ZOOM_MAX = 2.5;

export interface DagZoomResult {
  zoom: number;
  scrollLeft: number;
  scrollTop: number;
}

/** Where the DAG was last looked at, so leaving and returning restores the view. */
export type DagViewport = DagZoomResult;

interface DagZoomInput extends DagZoomResult {
  deltaY: number;
  focalX: number;
  focalY: number;
}

export function zoomDagAtPoint({
  zoom,
  deltaY,
  focalX,
  focalY,
  scrollLeft,
  scrollTop,
}: DagZoomInput): DagZoomResult {
  const nextZoom = clamp(zoom * Math.exp(-deltaY * 0.002), DAG_ZOOM_MIN, DAG_ZOOM_MAX);
  const ratio = nextZoom / zoom;
  return {
    zoom: nextZoom,
    scrollLeft: (scrollLeft + focalX) * ratio - focalX,
    scrollTop: (scrollTop + focalY) * ratio - focalY,
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

/** Frame the whole graph, so opening the DAG shows what exists rather than a corner of it.
 *
 * Returns null when there is nothing to frame yet. The result never magnifies
 * past 1: a small graph keeps its authored node size instead of ballooning to
 * fill the pane.
 */
export function fitDagToViewport({
  nodes,
  viewportWidth,
  viewportHeight,
  padding = 32,
}: DagFitInput): DagZoomResult | null {
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
  const usableHeight = Math.max(1, viewportHeight - padding * 2);
  const zoom = clamp(
    Math.min(usableWidth / contentWidth, usableHeight / contentHeight),
    DAG_ZOOM_MIN,
    1,
  );
  return {
    zoom,
    scrollLeft: Math.max(0, minX * zoom - (viewportWidth - contentWidth * zoom) / 2),
    scrollTop: Math.max(0, minY * zoom - (viewportHeight - contentHeight * zoom) / 2),
  };
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}
