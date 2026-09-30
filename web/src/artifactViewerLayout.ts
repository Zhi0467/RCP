import type { Point, Size } from "./floatingWindow";

export interface ViewerPlacement {
  mode: "docked" | "floating";
  fullscreen: boolean;
  collapsed: boolean;
  x: number;
  y: number;
  width: number;
  height: number;
}

export const defaultViewerPlacement: ViewerPlacement = {
  mode: "docked",
  fullscreen: false,
  collapsed: false,
  x: 40,
  y: 40,
  width: 640,
  height: 720,
};

export function parseViewerPlacement(raw: string | null): ViewerPlacement {
  try {
    const value = JSON.parse(raw ?? "null");
    if (
      value &&
      ["docked", "floating"].includes(value.mode) &&
      typeof value.fullscreen === "boolean" &&
      typeof value.collapsed === "boolean" &&
      [value.x, value.y, value.width, value.height].every(Number.isFinite) &&
      value.width > 0 &&
      value.height > 0
    )
      return value;
  } catch {
    /* A blocked or malformed preference never prevents viewing. */
  }
  return { ...defaultViewerPlacement };
}

export function viewerRect(placement: ViewerPlacement, viewport: Size) {
  const width = Math.min(viewport.width, Math.max(320, placement.width));
  const height = Math.min(viewport.height, Math.max(240, placement.height));
  if (placement.fullscreen) return { x: 0, y: 0, ...viewport };
  if (placement.mode === "docked")
    return { x: viewport.width - width, y: 0, width, height: viewport.height };
  return {
    x: Math.max(0, Math.min(placement.x, viewport.width - width)),
    y: Math.max(0, Math.min(placement.y, viewport.height - height)),
    width,
    height,
  };
}

export function floatViewer(placement: ViewerPlacement, viewport: Size): ViewerPlacement {
  const rect = viewerRect(placement, viewport);
  return { ...placement, x: rect.x, y: rect.y, mode: "floating", fullscreen: false };
}

export function moveViewer(
  placement: ViewerPlacement,
  delta: Point,
  viewport: Size,
): ViewerPlacement {
  const next = { ...placement, x: placement.x + delta.x, y: placement.y + delta.y };
  return { ...next, ...viewerRect(next, viewport) };
}

export function resizeViewer(
  placement: ViewerPlacement,
  deltaX: number,
  viewport: Size,
): ViewerPlacement {
  const rect = viewerRect(placement, viewport);
  const right = rect.x + rect.width;
  const width = Math.min(right, Math.max(Math.min(320, right), rect.width - deltaX));
  return { ...placement, width, x: right - width };
}

export function toggleViewerFullscreen(placement: ViewerPlacement): ViewerPlacement {
  return { ...placement, fullscreen: !placement.fullscreen };
}

export function collapseViewer(placement: ViewerPlacement, collapsed: boolean): ViewerPlacement {
  return collapsed || placement.collapsed
    ? { ...placement, collapsed, mode: "docked", fullscreen: false }
    : placement;
}

export function acceptsArtifactEditMessage(
  event: { source: unknown; origin: string; data: unknown },
  frame: unknown,
  origin: string,
  artifactId: string,
): boolean {
  if (!frame || event.source !== frame || event.origin !== origin) return false;
  const data = event.data as Record<string, unknown> | null;
  return Boolean(
    data &&
    data.type === "rcp-artifact-edit-started" &&
    data.version === 1 &&
    data.artifact_id === artifactId &&
    typeof data.operation_id === "string" &&
    data.operation_id,
  );
}

export function artifactVersionChanged(previous: string | null, current: string): boolean {
  return previous !== null && previous !== current;
}
