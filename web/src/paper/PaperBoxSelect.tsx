import { useEffect, useRef, type PointerEvent, type ReactNode } from "react";
import { boxWhere } from "./previewComments";

// Mirror the inline shell's text selection limit.
const BOX_TEXT_MAX_CHARS = 4096;

/** A box drawn on a PDF page or an image. */
export interface PaperBox {
  /** The text-layer text inside the box, or "Selected area". */
  text: string;
  where?: string;
  rect: DOMRect;
  /** Removes the drawn box. */
  clear: () => void;
}

type Drag = { target: HTMLElement; box: HTMLElement; x: number; y: number; pointer: number };

/** Lets a mouse or pen draw a box over each `[data-box-target]` inside; touch keeps scrolling.
 *
 * A drag that starts on text-layer text stays a native text selection. */
export function PaperBoxSelect({
  onBox,
  children,
}: {
  onBox?: (box: PaperBox) => void;
  children: ReactNode;
}) {
  const drag = useRef<Drag | null>(null);
  const drawn = useRef<HTMLElement | null>(null);
  useEffect(() => () => drawn.current?.remove(), []);

  const place = (state: Drag, clientX: number, clientY: number) => {
    const bounds = state.target.getBoundingClientRect();
    const x = Math.min(Math.max(clientX - bounds.left, 0), bounds.width);
    const y = Math.min(Math.max(clientY - bounds.top, 0), bounds.height);
    Object.assign(state.box.style, {
      left: `${Math.min(x, state.x)}px`,
      top: `${Math.min(y, state.y)}px`,
      width: `${Math.abs(x - state.x)}px`,
      height: `${Math.abs(y - state.y)}px`,
    });
  };

  return (
    <div
      className={`paper-box-select${onBox ? " enabled" : ""}`}
      onPointerDown={(event: PointerEvent<HTMLDivElement>) => {
        if (!onBox || event.pointerType === "touch" || event.button !== 0) return;
        const element = event.target as Element;
        const target = element.closest<HTMLElement>("[data-box-target]");
        if (!target?.firstElementChild || !event.currentTarget.contains(target)) return;
        const text = element.closest(".textLayer span");
        if (text && text.getAttribute("role") !== "img") return;
        event.preventDefault();
        window.getSelection()?.removeAllRanges();
        drawn.current?.remove();
        const bounds = target.getBoundingClientRect();
        const box = document.createElement("div");
        box.className = "paper-select-box";
        target.append(box);
        drawn.current = box;
        drag.current = {
          target,
          box,
          x: event.clientX - bounds.left,
          y: event.clientY - bounds.top,
          pointer: event.pointerId,
        };
        place(drag.current, event.clientX, event.clientY);
        target.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        const state = drag.current;
        if (state?.pointer === event.pointerId) place(state, event.clientX, event.clientY);
      }}
      onPointerUp={(event) => {
        const state = drag.current;
        if (!state || state.pointer !== event.pointerId) return;
        drag.current = null;
        place(state, event.clientX, event.clientY);
        const rect = state.box.getBoundingClientRect();
        if (rect.width < 4 || rect.height < 4 || !onBox) {
          state.box.remove();
          return;
        }
        const box = state.box;
        onBox({
          text: boxText(state.target, rect),
          where: boxWhere(state.target, rect),
          rect,
          clear: () => box.remove(),
        });
      }}
      onPointerCancel={() => {
        drag.current?.box.remove();
        drag.current = null;
      }}
    >
      {children}
    </div>
  );
}

/** The text-layer runs whose centers fall inside the box, in reading order. */
function boxText(target: HTMLElement, rect: DOMRect): string {
  const text = [...target.querySelectorAll<HTMLElement>(".textLayer span")]
    .filter((span) => {
      const box = span.getBoundingClientRect();
      const x = box.left + box.width / 2;
      const y = box.top + box.height / 2;
      return box.width && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
    })
    .map((span) => span.textContent ?? "")
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "Selected area";
  return text.length > BOX_TEXT_MAX_CHARS ? `${text.slice(0, BOX_TEXT_MAX_CHARS - 1)}…` : text;
}
