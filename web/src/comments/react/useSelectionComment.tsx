import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MessageCirclePlus } from "lucide-react";
import type { Point } from "../box.ts";
import { chatAnnotationViewportMetrics, chatSelectionCommentPosition } from "../../chat/chatInput";

/** A finished selection that can start a thread: where to offer Comment and what to open. */
export interface SelectionCommentOffer {
  rect: { left: number; right: number; top: number; bottom: number };
  /** The selection's first line, which the offer sits above; the whole rect without it. */
  firstLine?: { left: number; right: number; top: number };
  open: () => void;
}

const BUTTON = { width: 96, height: 26 };

/** Offers Comment above a finished selection that `resolve` accepts, as chat does; the
 *  composer opens only when asked, so the platform's Copy stays usable. A selection
 *  still being dragged offers nothing, and a resize moves the offer with it. */
export function useSelectionComment(
  enabled: boolean,
  resolve: (selection: Selection) => SelectionCommentOffer | null,
) {
  const [offer, setOffer] = useState<SelectionCommentOffer | null>(null);
  const resolveRef = useRef(resolve);
  resolveRef.current = resolve;
  useEffect(() => {
    if (!enabled) {
      setOffer(null);
      return;
    }
    let frame = 0;
    let pressing = false;
    const read = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (pressing) return;
        const selection = window.getSelection();
        setOffer(
          selection && !selection.isCollapsed && selection.rangeCount
            ? resolveRef.current(selection)
            : null,
        );
      });
    };
    const press = () => {
      pressing = true;
    };
    const release = () => {
      pressing = false;
      read();
    };
    const changed = () => {
      if (window.getSelection()?.isCollapsed) setOffer(null);
      else read();
    };
    document.addEventListener("pointerdown", press, true);
    document.addEventListener("pointerup", release, true);
    document.addEventListener("keyup", read);
    document.addEventListener("selectionchange", changed);
    window.addEventListener("resize", read);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("pointerdown", press, true);
      document.removeEventListener("pointerup", release, true);
      document.removeEventListener("keyup", read);
      document.removeEventListener("selectionchange", changed);
      window.removeEventListener("resize", read);
    };
  }, [enabled]);
  return { offer, setOffer };
}

/** Where the offer sits: above the selection's first line, or below it without room. */
export function offerPoint(
  offer: SelectionCommentOffer,
  button: { width: number; height: number } = BUTTON,
): Point {
  const { left, top } = chatSelectionCommentPosition(
    { firstLine: offer.firstLine ?? offer.rect, bottom: offer.rect.bottom },
    chatAnnotationViewportMetrics(
      { width: window.innerWidth, height: window.innerHeight },
      window.visualViewport,
    ),
    button,
  );
  return { x: left, y: top };
}

/** The first line of a range, which the offer sits above. */
export function firstLine(range: Range) {
  return [...range.getClientRects()].find((rect) => rect.width && rect.height);
}

export function SelectionCommentButton({
  offer,
  onOpen,
}: {
  offer: SelectionCommentOffer | null;
  onOpen: () => void;
}) {
  const button = useRef<HTMLButtonElement>(null);
  const [size, setSize] = useState(BUTTON);
  useLayoutEffect(() => {
    const box = button.current?.getBoundingClientRect();
    if (box?.width && (box.width !== size.width || box.height !== size.height))
      setSize({ width: box.width, height: box.height });
  }, [offer, size.width, size.height]);
  if (!offer) return null;
  const point = offerPoint(offer, size);
  return createPortal(
    <button
      ref={button}
      type="button"
      className="chat-selection-comment"
      style={{ left: point.x, top: point.y }}
      onPointerDown={(event) => event.preventDefault()}
      onMouseDown={(event) => event.preventDefault()}
      onClick={() => {
        onOpen();
        offer.open();
      }}
    >
      <MessageCirclePlus size={13} /> Comment
    </button>,
    document.body,
  );
}
