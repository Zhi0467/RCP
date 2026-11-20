import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Point } from "../box.ts";
import type { CommentThread } from "../types.ts";
import { clearCommentHighlight, showCommentHighlight } from "./commentHighlight.ts";
import type { MarginPoint } from "./domText.ts";
import { pinNumbers } from "./threadModel.ts";
import "../comments.css";

/** Red numbered pins for each open thread's anchor, in window coordinates: in the
 *  text's right margin for a quote, at the corner for a box. Quoted text shows in
 *  soft red. They follow scrolling and resizing, and `revision` re-places them
 *  after the target's text or layout changes. */
export function CommentPins({
  threads,
  position,
  range,
  selected,
  onPick,
  revision,
  subscribe,
}: {
  threads: readonly CommentThread[];
  position: (thread: CommentThread) => Point | MarginPoint | null;
  /** The quoted text of a text thread, highlighted wherever it shows. */
  range?: (thread: CommentThread) => Range | null;
  selected: string | null;
  onPick: (thread: CommentThread) => void;
  revision?: unknown;
  /** An outside change, such as an edit, that moves anchors without scrolling. */
  subscribe?: (listener: () => void) => () => void;
}) {
  const [points, setPoints] = useState<Map<string, Point | MarginPoint>>(() => new Map());
  const owner = useRef({});
  const place = useRef<() => void>(() => undefined);
  place.current = () => {
    const next = new Map<string, Point | MarginPoint>();
    const quoted: Range[] = [];
    const chosen: Range[] = [];
    for (const thread of threads) {
      if (thread.state !== "open") continue;
      const point = position(thread);
      if (point) next.set(thread.thread_id, point);
      const quote = range?.(thread);
      if (quote) (thread.thread_id === selected ? chosen : quoted).push(quote);
    }
    if (range) showCommentHighlight(owner.current, quoted, chosen);
    stackSharedLines(next);
    setPoints((current) => (samePoints(current, next) ? current : next));
  };
  useLayoutEffect(() => place.current());
  useEffect(() => {
    const self = owner.current;
    return () => clearCommentHighlight(self);
  }, []);
  useEffect(() => {
    let frame = 0;
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => place.current());
    };
    window.addEventListener("resize", schedule);
    document.addEventListener("scroll", schedule, { capture: true });
    const unsubscribe = subscribe?.(schedule);
    return () => {
      unsubscribe?.();
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", schedule);
      document.removeEventListener("scroll", schedule, { capture: true });
    };
  }, [subscribe]);
  useEffect(() => {
    const frame = requestAnimationFrame(() => place.current());
    return () => cancelAnimationFrame(frame);
  }, [revision, threads]);
  if (typeof document === "undefined") return null;
  const numbers = pinNumbers(threads);
  return createPortal(
    <div className="rcp-comment-pins">
      {threads.map((thread) => {
        const point = points.get(thread.thread_id);
        const number = numbers.get(thread.thread_id);
        if (!point || !number) return null;
        return (
          <button
            key={thread.thread_id}
            type="button"
            className={`rcp-comment-pin${"margin" in point ? " margin" : ""}${selected === thread.thread_id ? " selected" : ""}`}
            aria-label={`Comment ${number}`}
            style={{ left: point.x, top: point.y }}
            onClick={() => onPick(thread)}
          >
            {number}
          </button>
        );
      })}
    </div>,
    document.body,
  );
}

const PIN_STEP = 24;

/** Margin pins on one line stack in the margin, centred on that line, so none hides another. */
function stackSharedLines(points: Map<string, Point | MarginPoint>) {
  const lines: [string, MarginPoint][][] = [];
  for (const entry of points) {
    const [, point] = entry;
    if (!("margin" in point)) continue;
    const line = lines.find(
      ([[, first]]) => first.x === point.x && Math.abs(first.y - point.y) < PIN_STEP / 2,
    );
    if (line) line.push(entry as [string, MarginPoint]);
    else lines.push([entry as [string, MarginPoint]]);
  }
  for (const line of lines) {
    const centre = line[0][1].y;
    line.forEach(([id, point], index) =>
      points.set(id, { ...point, y: centre + (index - (line.length - 1) / 2) * PIN_STEP }),
    );
  }
}

function samePoints(a: Map<string, Point>, b: Map<string, Point | MarginPoint>) {
  if (a.size !== b.size) return false;
  for (const [id, point] of b) {
    const old = a.get(id);
    if (!old || Math.abs(old.x - point.x) > 0.5 || Math.abs(old.y - point.y) > 0.5) return false;
  }
  return true;
}
