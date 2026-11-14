import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Point } from "../box.ts";
import type { CommentThread } from "../types.ts";
import { pinNumbers } from "./threadModel.ts";
import "../comments.css";

/** Red numbered pins at the end of each open thread's anchor, in window coordinates.
 *  They follow scrolling and resizing, and `revision` re-places them after the
 *  target's text or layout changes. */
export function CommentPins({
  threads,
  position,
  selected,
  onPick,
  revision,
  subscribe,
}: {
  threads: readonly CommentThread[];
  position: (thread: CommentThread) => Point | null;
  selected: string | null;
  onPick: (thread: CommentThread) => void;
  revision?: unknown;
  /** An outside change, such as an edit, that moves anchors without scrolling. */
  subscribe?: (listener: () => void) => () => void;
}) {
  const [points, setPoints] = useState<Map<string, Point>>(() => new Map());
  const place = useRef<() => void>(() => undefined);
  place.current = () => {
    const next = new Map<string, Point>();
    for (const thread of threads) {
      if (thread.state !== "open") continue;
      const point = position(thread);
      if (point) next.set(thread.thread_id, point);
    }
    setPoints((current) => (samePoints(current, next) ? current : next));
  };
  useLayoutEffect(() => place.current());
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
            className={`rcp-comment-pin${selected === thread.thread_id ? " selected" : ""}`}
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

function samePoints(a: Map<string, Point>, b: Map<string, Point>) {
  if (a.size !== b.size) return false;
  for (const [id, point] of b) {
    const old = a.get(id);
    if (!old || Math.abs(old.x - point.x) > 0.5 || Math.abs(old.y - point.y) > 0.5) return false;
  }
  return true;
}
