export interface Point {
  x: number;
  y: number;
}
export interface Size {
  width: number;
  height: number;
}

export const FLOATING_WINDOW_MARGIN = 12;
export const FLOATING_WINDOW_GAP = 12;
export const FLOATING_WINDOW_TOP = 118;

export function clampFloatingPosition(
  position: Point,
  windowSize: Size,
  viewport: Size,
  margin = FLOATING_WINDOW_MARGIN,
): Point {
  const maxX = Math.max(
    margin,
    viewport.width - Math.min(windowSize.width, viewport.width) - margin,
  );
  const maxY = Math.max(
    margin,
    viewport.height - Math.min(windowSize.height, viewport.height) - margin,
  );
  return {
    x: Math.min(maxX, Math.max(margin, position.x)),
    y: Math.min(maxY, Math.max(margin, position.y)),
  };
}

export function clampFloatingSize(
  size: Size,
  viewport: Size,
  minimum: Size,
  margin = FLOATING_WINDOW_MARGIN,
): Size {
  const maximum = {
    width: Math.max(0, viewport.width - margin * 2),
    height: Math.max(0, viewport.height - margin * 2),
  };
  return {
    width: Math.min(maximum.width, Math.max(minimum.width, size.width)),
    height: Math.min(maximum.height, Math.max(minimum.height, size.height)),
  };
}

export function resizedFloatingSize(origin: Size, delta: Point): Size {
  return {
    width: origin.width + delta.x,
    height: origin.height + delta.y,
  };
}

export function parseFloatingSize(value: string | null): Size | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      !("width" in parsed) ||
      !("height" in parsed) ||
      typeof parsed.width !== "number" ||
      typeof parsed.height !== "number" ||
      !Number.isFinite(parsed.width) ||
      !Number.isFinite(parsed.height) ||
      parsed.width <= 0 ||
      parsed.height <= 0
    )
      return null;
    return { width: parsed.width, height: parsed.height };
  } catch {
    return null;
  }
}

export function nodeDetailSizeStorageKey(projectId: string): string {
  return `rcp:node-detail-size:${projectId}`;
}

export function movedPosition(origin: Point, pointerOrigin: Point, pointer: Point): Point {
  return {
    x: origin.x + pointer.x - pointerOrigin.x,
    y: origin.y + pointer.y - pointerOrigin.y,
  };
}

export function floatingWindowSize(kind: "detail" | "chat", viewport: Size): Size {
  const maximumWidth = kind === "detail" ? 590 : 620;
  const sharedWidth = Math.max(
    280,
    (viewport.width - FLOATING_WINDOW_MARGIN * 2 - FLOATING_WINDOW_GAP) / 2,
  );
  return {
    width: Math.min(maximumWidth, sharedWidth),
    height: Math.min(
      720,
      Math.max(240, viewport.height - FLOATING_WINDOW_TOP - FLOATING_WINDOW_MARGIN),
    ),
  };
}

export function defaultFloatingPosition(kind: "detail" | "chat", viewport: Size): Point {
  if (kind === "chat") return { x: FLOATING_WINDOW_MARGIN, y: FLOATING_WINDOW_TOP };
  const width = floatingWindowSize(kind, viewport).width;
  return { x: viewport.width - width - FLOATING_WINDOW_MARGIN, y: FLOATING_WINDOW_TOP };
}
