import { useEffect, useRef, useState, type RefObject } from "react";
import type { Point } from "../comments/box.ts";
import type { CommentThread } from "../comments/types.ts";
import { CommentPins } from "../comments/react/CommentPins";
import { createFrameLocator } from "../comments/viewerFrame.ts";

export type PickInlineThread = (
  thread: CommentThread,
  point: Point | null,
  locate: () => void,
) => void;

export function useInlineArtifactPins(
  frame: RefObject<HTMLIFrameElement | null>,
  threads: readonly CommentThread[],
) {
  const [revision, setRevision] = useState(0);
  const locator = useRef<{
    frame: HTMLIFrameElement;
    api: ReturnType<typeof createFrameLocator>;
  } | null>(null);
  const current = () => {
    const element = frame.current;
    if (!element) return null;
    if (locator.current?.frame !== element)
      locator.current = {
        frame: element,
        api: createFrameLocator(element, () => setRevision((value) => value + 1)),
      };
    return locator.current.api;
  };
  const open = threads.filter((thread) => thread.state === "open");
  const openKey = JSON.stringify(open.map((thread) => [thread.thread_id, thread.anchor]));
  useEffect(() => {
    current()?.track(open);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the key names the open anchors
  }, [openKey]);
  useEffect(() => {
    const receive = (event: MessageEvent) => current()?.receive(event);
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reads the frame ref when called
  }, []);
  const position = (thread: CommentThread) => current()?.position(thread.thread_id) ?? null;
  return {
    onLoad: () => {
      const api = current();
      api?.track(open);
      api?.reload();
    },
    pins: (selected: string | null, onPick: PickInlineThread) =>
      open.length > 0 && (
        <CommentPins
          threads={open}
          position={position}
          selected={selected}
          revision={revision}
          onPick={(thread) => onPick(thread, position(thread), () => current()?.locate(thread))}
        />
      ),
  };
}
