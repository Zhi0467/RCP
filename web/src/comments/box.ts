export interface Point {
  x: number;
  y: number;
}
export interface Box extends Point {
  width: number;
  height: number;
}

export function boxFractions(start: Point, end: Point, bounds: Box): Box | null {
  if (bounds.width <= 0 || bounds.height <= 0) return null;
  const clamp = (value: number) => Math.max(0, Math.min(1, value));
  const x = clamp((Math.min(start.x, end.x) - bounds.x) / bounds.width);
  const y = clamp((Math.min(start.y, end.y) - bounds.y) / bounds.height);
  const right = clamp((Math.max(start.x, end.x) - bounds.x) / bounds.width);
  const bottom = clamp((Math.max(start.y, end.y) - bounds.y) / bounds.height);
  return right > x && bottom > y ? { x, y, width: right - x, height: bottom - y } : null;
}

export function installBoxTool(surface: HTMLElement, publish: (box: Box) => void) {
  const abort = new AbortController();
  let start: Point | null = null;
  let active = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const mark = surface.ownerDocument.createElement("div");
  mark.className = "rcp-comment-box";
  const clear = () => {
    clearTimeout(timer);
    start = null;
    active = false;
    mark.remove();
  };
  surface.addEventListener(
    "pointerdown",
    (event) => {
      if (!event.isPrimary || event.button !== 0) return;
      clear();
      start = { x: event.clientX, y: event.clientY };
      const activate = () => {
        active = true;
        surface.setPointerCapture(event.pointerId);
        surface.ownerDocument.body.append(mark);
      };
      if (event.pointerType === "touch") timer = setTimeout(activate, 450);
      else activate();
    },
    { signal: abort.signal },
  );
  surface.addEventListener(
    "pointermove",
    (event) => {
      if (!start) return;
      if (!active) {
        if (Math.hypot(event.clientX - start.x, event.clientY - start.y) > 8) clear();
        return;
      }
      event.preventDefault();
      Object.assign(mark.style, {
        left: `${Math.min(start.x, event.clientX)}px`,
        top: `${Math.min(start.y, event.clientY)}px`,
        width: `${Math.abs(start.x - event.clientX)}px`,
        height: `${Math.abs(start.y - event.clientY)}px`,
      });
    },
    { signal: abort.signal },
  );
  surface.addEventListener(
    "pointerup",
    (event) => {
      if (start && active) {
        const rect = boxFractions(
          start,
          { x: event.clientX, y: event.clientY },
          surface.getBoundingClientRect(),
        );
        if (rect) publish(rect);
      }
      clear();
    },
    { signal: abort.signal },
  );
  surface.addEventListener("pointercancel", clear, { signal: abort.signal });
  return () => {
    clear();
    abort.abort();
  };
}
