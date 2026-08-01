import { useEffect, useRef, useState } from "react";
import {
  clampFloatingPosition,
  defaultFloatingPosition,
  movedPosition,
  type Point,
} from "../floatingWindow";

let topFloatingZIndex = 110;

interface Props {
  children: React.ReactNode;
  className: string;
  kind: "detail" | "chat";
}

export function DraggableWindow({ children, className, kind }: Props) {
  const root = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<Point>(() => defaultFloatingPosition(kind, {
    width: window.innerWidth,
    height: window.innerHeight,
  }));
  const [zIndex, setZIndex] = useState(() => ++topFloatingZIndex);
  const drag = useRef<{ origin: Point; pointer: Point } | null>(null);

  const clamp = (next: Point) => {
    const bounds = root.current?.getBoundingClientRect();
    return clampFloatingPosition(
      next,
      { width: bounds?.width ?? 0, height: bounds?.height ?? 0 },
      { width: window.innerWidth, height: window.innerHeight },
    );
  };

  useEffect(() => {
    const onResize = () => setPosition((current) => clamp(current));
    window.addEventListener("resize", onResize);
    onResize();
    return () => window.removeEventListener("resize", onResize);
  }, []);

  return (
    <div
      ref={root}
      className={`floating-window ${className}`}
      style={{ left: position.x, top: position.y, zIndex }}
      onPointerDownCapture={() => setZIndex(++topFloatingZIndex)}
      onFocusCapture={() => setZIndex(++topFloatingZIndex)}
      onPointerDown={(event) => {
        const target = event.target as HTMLElement;
        if (!target.closest("[data-drag-handle]")) return;
        if (target.closest("button, input, select, textarea, a")) return;
        drag.current = { origin: position, pointer: { x: event.clientX, y: event.clientY } };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (!drag.current) return;
        setPosition(clamp(movedPosition(
          drag.current.origin,
          drag.current.pointer,
          { x: event.clientX, y: event.clientY },
        )));
      }}
      onPointerUp={(event) => {
        drag.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
    >
      {children}
    </div>
  );
}
