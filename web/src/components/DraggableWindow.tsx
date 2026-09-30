import { useEffect, useRef, useState } from "react";
import {
  clampFloatingPosition,
  clampFloatingSize,
  defaultFloatingPosition,
  detailWindowSlotPosition,
  floatingWindowSize,
  movedPosition,
  parseFloatingSize,
  resizedFloatingRect,
  type DetailWindowSlot,
  type Point,
  type ResizeCorner,
  type Size,
} from "../floatingWindow";
import { NODE_DETAIL_RESIZE_MIN_HEIGHT, NODE_DETAIL_RESIZE_MIN_WIDTH } from "../uiConstants";

import {
  floatViewer,
  moveViewer,
  resizeViewer,
  toggleViewerFullscreen,
  viewerRect,
  type ViewerPlacement,
} from "../artifactViewerLayout";

let topFloatingZIndex = 110;

interface Props {
  children: React.ReactNode;
  className: string;
  kind: "detail" | "chat";
  resizable?: boolean;
  sizeStorageKey?: string;
  detailSlot?: DetailWindowSlot;
  focusRequestToken?: string | number;
  viewer?: { placement: ViewerPlacement; onChange: (placement: ViewerPlacement) => void };
}

export function shouldStartWindowDrag(target: Element): boolean {
  if (!target.closest("[data-drag-handle]")) return false;
  if (target.closest("[data-text-selectable]")) return false;
  return !target.closest("button, input, select, textarea, a");
}

const detailMinimumSize: Size = {
  width: NODE_DETAIL_RESIZE_MIN_WIDTH,
  height: NODE_DETAIL_RESIZE_MIN_HEIGHT,
};

export function DraggableWindow({
  children,
  className,
  kind,
  resizable = false,
  sizeStorageKey,
  detailSlot,
  focusRequestToken,
  viewer,
}: Props) {
  const root = useRef<HTMLDivElement>(null);
  const preferredSize = useRef<Size | null>(null);
  const [size, setSize] = useState<Size | null>(() => {
    if (!resizable) return null;
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    let stored: Size | null = null;
    if (sizeStorageKey) {
      try {
        stored = parseFloatingSize(window.localStorage.getItem(sizeStorageKey));
      } catch {
        stored = null;
      }
    }
    preferredSize.current = stored ?? floatingWindowSize(kind, viewport);
    return clampFloatingSize(preferredSize.current, viewport, detailMinimumSize);
  });
  const [position, setPosition] = useState<Point>(() => {
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    const initialSize = size ?? floatingWindowSize(kind, viewport);
    if (kind === "detail" && detailSlot) {
      return detailWindowSlotPosition(detailSlot, initialSize, viewport);
    }
    return clampFloatingPosition(defaultFloatingPosition(kind, viewport), initialSize, viewport);
  });
  const [zIndex, setZIndex] = useState(() => ++topFloatingZIndex);
  const [viewport, setViewport] = useState(() => ({
    width: window.innerWidth,
    height: window.innerHeight,
  }));
  const viewerDrag = useRef<{ placement: ViewerPlacement; pointer: Point } | null>(null);
  const viewerResize = useRef<{ placement: ViewerPlacement; x: number } | null>(null);
  // Pointer capture retargets the following dblclick to this root, so the handle
  // check happens on the press, where the target is still the pressed element.
  const pressedHandle = useRef(false);
  const drag = useRef<{ origin: Point; pointer: Point } | null>(null);
  const resize = useRef<{
    corner: ResizeCorner;
    position: Point;
    size: Size;
    pointer: Point;
  } | null>(null);

  const clamp = (next: Point, nextSize?: Size | null) => {
    const bounds = root.current?.getBoundingClientRect();
    return clampFloatingPosition(
      next,
      nextSize ?? { width: bounds?.width ?? 0, height: bounds?.height ?? 0 },
      { width: window.innerWidth, height: window.innerHeight },
    );
  };

  const applyUserResize = (pointer: Point) => {
    if (!resize.current) return;
    const next = resizedFloatingRect(
      { position: resize.current.position, size: resize.current.size },
      {
        x: pointer.x - resize.current.pointer.x,
        y: pointer.y - resize.current.pointer.y,
      },
      resize.current.corner,
      { width: window.innerWidth, height: window.innerHeight },
      detailMinimumSize,
    );
    preferredSize.current = next.size;
    setSize(next.size);
    setPosition(next.position);
    if (sizeStorageKey) {
      try {
        window.localStorage.setItem(sizeStorageKey, JSON.stringify(next.size));
      } catch {
        // Resizing remains usable when browser storage is unavailable.
      }
    }
  };

  useEffect(() => {
    const onResize = () => {
      const viewport = { width: window.innerWidth, height: window.innerHeight };
      setViewport(viewport);
      const nextSize =
        resizable && preferredSize.current
          ? clampFloatingSize(preferredSize.current, viewport, detailMinimumSize)
          : null;
      if (nextSize) setSize(nextSize);
      if (kind === "detail" && detailSlot) {
        setPosition(
          detailWindowSlotPosition(
            detailSlot,
            nextSize ?? floatingWindowSize(kind, viewport),
            viewport,
          ),
        );
      } else {
        setPosition((current) => clamp(current, nextSize));
      }
    };
    window.addEventListener("resize", onResize);
    onResize();
    return () => window.removeEventListener("resize", onResize);
  }, [detailSlot, kind, resizable]);

  useEffect(() => {
    if (focusRequestToken === undefined) return;
    setZIndex(++topFloatingZIndex);
  }, [focusRequestToken]);

  const panelRect = viewer ? viewerRect(viewer.placement, viewport) : null;

  return (
    <div
      ref={root}
      className={`floating-window ${className}`}
      style={{
        left: position.x,
        top: position.y,
        zIndex,
        ...(size ? { width: size.width, height: size.height } : {}),
        ...(panelRect
          ? {
              left: panelRect.x,
              top: panelRect.y,
              width: panelRect.width,
              height: panelRect.height,
              maxHeight: "100vh",
            }
          : {}),
      }}
      onDoubleClick={() => {
        if (viewer && pressedHandle.current) {
          viewer.onChange(toggleViewerFullscreen(viewer.placement));
        }
      }}
      onPointerDownCapture={() => setZIndex(++topFloatingZIndex)}
      onFocusCapture={() => setZIndex(++topFloatingZIndex)}
      onPointerDown={(event) => {
        const target = event.target as HTMLElement;
        pressedHandle.current = shouldStartWindowDrag(target);
        if (!pressedHandle.current) return;
        if (viewer) {
          viewerDrag.current = {
            placement: floatViewer(viewer.placement, viewport),
            pointer: { x: event.clientX, y: event.clientY },
          };
          event.currentTarget.setPointerCapture(event.pointerId);
          return;
        }
        drag.current = { origin: position, pointer: { x: event.clientX, y: event.clientY } };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (viewer && viewerDrag.current) {
          const delta = {
            x: event.clientX - viewerDrag.current.pointer.x,
            y: event.clientY - viewerDrag.current.pointer.y,
          };
          if (Math.abs(delta.x) + Math.abs(delta.y) > 3)
            viewer.onChange(moveViewer(viewerDrag.current.placement, delta, viewport));
          return;
        }
        if (!drag.current) return;
        setPosition(
          clamp(
            movedPosition(drag.current.origin, drag.current.pointer, {
              x: event.clientX,
              y: event.clientY,
            }),
          ),
        );
      }}
      onPointerCancel={() => {
        viewerDrag.current = null;
        drag.current = null;
      }}
      onLostPointerCapture={() => {
        viewerDrag.current = null;
        drag.current = null;
      }}
      onPointerUp={(event) => {
        viewerDrag.current = null;
        drag.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
    >
      {children}
      {viewer && !viewer.placement.fullscreen && (
        <div
          className="artifact-viewer-resize"
          role="separator"
          aria-label="Viewer width"
          aria-orientation="vertical"
          aria-valuenow={panelRect!.width}
          aria-valuemin={Math.min(320, panelRect!.x + panelRect!.width)}
          aria-valuemax={panelRect!.x + panelRect!.width}
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
            event.preventDefault();
            viewer.onChange(
              resizeViewer(viewer.placement, event.key === "ArrowLeft" ? -24 : 24, viewport),
            );
          }}
          onPointerDown={(event) => {
            event.stopPropagation();
            viewerResize.current = { placement: viewer.placement, x: event.clientX };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            if (!viewerResize.current) return;
            viewer.onChange(
              resizeViewer(
                viewerResize.current.placement,
                event.clientX - viewerResize.current.x,
                viewport,
              ),
            );
          }}
          onPointerUp={(event) => {
            viewerResize.current = null;
            if (event.currentTarget.hasPointerCapture(event.pointerId))
              event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onPointerCancel={() => {
            viewerResize.current = null;
          }}
          onLostPointerCapture={() => {
            viewerResize.current = null;
          }}
        />
      )}
      {resizable &&
        size &&
        (["top-left", "top-right", "bottom-left", "bottom-right"] as const).map((corner) => (
          <div
            key={corner}
            className={`floating-window-resize-corner ${corner}`}
            data-resize-corner={corner}
            onPointerDown={(event) => {
              event.stopPropagation();
              resize.current = {
                corner,
                position,
                size,
                pointer: { x: event.clientX, y: event.clientY },
              };
              event.currentTarget.setPointerCapture(event.pointerId);
            }}
            onPointerMove={(event) => {
              if (!resize.current) return;
              event.stopPropagation();
              applyUserResize({ x: event.clientX, y: event.clientY });
            }}
            onPointerUp={(event) => {
              event.stopPropagation();
              resize.current = null;
              if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                event.currentTarget.releasePointerCapture(event.pointerId);
              }
            }}
            onPointerCancel={() => {
              resize.current = null;
            }}
            onLostPointerCapture={() => {
              resize.current = null;
            }}
          />
        ))}
    </div>
  );
}
