import { useEffect, useRef, useState } from "react";
import {
  clampFloatingPosition,
  clampFloatingSize,
  defaultFloatingPosition,
  floatingWindowSize,
  movedPosition,
  parseFloatingSize,
  resizedFloatingSize,
  type Point,
  type Size,
} from "../floatingWindow";
import {
  NODE_DETAIL_RESIZE_KEYBOARD_STEP,
  NODE_DETAIL_RESIZE_MIN_HEIGHT,
  NODE_DETAIL_RESIZE_MIN_WIDTH,
} from "../uiConstants";

let topFloatingZIndex = 110;

interface Props {
  children: React.ReactNode;
  className: string;
  kind: "detail" | "chat";
  resizable?: boolean;
  sizeStorageKey?: string;
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
  const [position, setPosition] = useState<Point>(() =>
    clampFloatingPosition(
      defaultFloatingPosition(kind, {
        width: window.innerWidth,
        height: window.innerHeight,
      }),
      size ??
        floatingWindowSize(kind, {
          width: window.innerWidth,
          height: window.innerHeight,
        }),
      { width: window.innerWidth, height: window.innerHeight },
    ),
  );
  const [zIndex, setZIndex] = useState(() => ++topFloatingZIndex);
  const drag = useRef<{ origin: Point; pointer: Point } | null>(null);
  const resize = useRef<{ origin: Size; pointer: Point } | null>(null);

  const clamp = (next: Point, nextSize?: Size | null) => {
    const bounds = root.current?.getBoundingClientRect();
    return clampFloatingPosition(
      next,
      nextSize ?? { width: bounds?.width ?? 0, height: bounds?.height ?? 0 },
      { width: window.innerWidth, height: window.innerHeight },
    );
  };

  const applyUserSize = (requested: Size) => {
    const next = clampFloatingSize(
      requested,
      { width: window.innerWidth, height: window.innerHeight },
      detailMinimumSize,
    );
    preferredSize.current = next;
    setSize(next);
    setPosition((current) => clamp(current, next));
    if (sizeStorageKey) {
      try {
        window.localStorage.setItem(sizeStorageKey, JSON.stringify(next));
      } catch {
        // Resizing remains usable when browser storage is unavailable.
      }
    }
  };

  useEffect(() => {
    const onResize = () => {
      const nextSize =
        resizable && preferredSize.current
          ? clampFloatingSize(
              preferredSize.current,
              { width: window.innerWidth, height: window.innerHeight },
              detailMinimumSize,
            )
          : null;
      if (nextSize) setSize(nextSize);
      setPosition((current) => clamp(current, nextSize));
    };
    window.addEventListener("resize", onResize);
    onResize();
    return () => window.removeEventListener("resize", onResize);
  }, [resizable]);

  return (
    <div
      ref={root}
      className={`floating-window ${className}`}
      style={{
        left: position.x,
        top: position.y,
        zIndex,
        ...(size ? { width: size.width, height: size.height } : {}),
      }}
      onPointerDownCapture={() => setZIndex(++topFloatingZIndex)}
      onFocusCapture={() => setZIndex(++topFloatingZIndex)}
      onPointerDown={(event) => {
        const target = event.target as HTMLElement;
        if (!shouldStartWindowDrag(target)) return;
        drag.current = { origin: position, pointer: { x: event.clientX, y: event.clientY } };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
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
      onPointerUp={(event) => {
        drag.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
    >
      {children}
      {resizable && size && (
        <button
          type="button"
          className="floating-window-resize-handle"
          aria-label="Resize node detail window"
          aria-keyshortcuts="ArrowLeft ArrowRight ArrowUp ArrowDown"
          title="Resize node detail window. Use arrow keys for keyboard resizing."
          onPointerDown={(event) => {
            event.stopPropagation();
            resize.current = {
              origin: size,
              pointer: { x: event.clientX, y: event.clientY },
            };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            if (!resize.current) return;
            event.stopPropagation();
            applyUserSize(
              resizedFloatingSize(resize.current.origin, {
                x: event.clientX - resize.current.pointer.x,
                y: event.clientY - resize.current.pointer.y,
              }),
            );
          }}
          onPointerUp={(event) => {
            resize.current = null;
            if (event.currentTarget.hasPointerCapture(event.pointerId)) {
              event.currentTarget.releasePointerCapture(event.pointerId);
            }
          }}
          onPointerCancel={() => {
            resize.current = null;
          }}
          onKeyDown={(event) => {
            const delta = {
              ArrowLeft: { x: -NODE_DETAIL_RESIZE_KEYBOARD_STEP, y: 0 },
              ArrowRight: { x: NODE_DETAIL_RESIZE_KEYBOARD_STEP, y: 0 },
              ArrowUp: { x: 0, y: -NODE_DETAIL_RESIZE_KEYBOARD_STEP },
              ArrowDown: { x: 0, y: NODE_DETAIL_RESIZE_KEYBOARD_STEP },
            }[event.key];
            if (!delta) return;
            event.preventDefault();
            applyUserSize(resizedFloatingSize(size, delta));
          }}
        />
      )}
    </div>
  );
}
