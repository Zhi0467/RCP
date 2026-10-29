import { useEffect, useRef, type PointerEvent, type ReactNode } from "react";
import { boxWhere } from "./previewComments";

/** A box drawn on a PDF page or an image. */
export interface PaperBox {
  /** The text-layer text inside the box; empty when it holds none. */
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
  const cancel = () => {
    drag.current?.box.remove();
    drag.current = null;
  };
  // Escape abandons a box still being drawn, as it does a native selection.
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && drag.current) cancel();
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, []);

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
      onPointerCancel={cancel}
    >
      {children}
    </div>
  );
}

/** The text-layer characters whose centers fall inside the box, in reading order.
 *
 * pdf.js often lays a whole line out as one run, so a box over part of a line is
 * judged character by character. */
function boxText(target: HTMLElement, rect: DOMRect): string {
  return [...target.querySelectorAll<HTMLElement>(".textLayer span")]
    .map((span) => textInside(span, rect))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

/** One text-layer run's characters whose centers fall inside `rect`. */
function textInside(span: HTMLElement, rect: DOMRect): string {
  const run = span.firstChild;
  if (!(run instanceof Text) || !overlaps(span.getBoundingClientRect(), rect)) return "";
  const character = document.createRange();
  let kept = "";
  for (let index = 0; index < run.length; index++) {
    character.setStart(run, index);
    character.setEnd(run, index + 1);
    if (centerInside(character.getBoundingClientRect(), rect)) kept += run.data[index];
  }
  return kept;
}

const overlaps = (box: DOMRect, rect: DOMRect) =>
  box.right > rect.left && box.left < rect.right && box.bottom > rect.top && box.top < rect.bottom;

function centerInside(box: DOMRect, rect: DOMRect) {
  const x = box.left + box.width / 2;
  const y = box.top + box.height / 2;
  return box.width > 0 && x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
}
