import type { CommentAnchor, CommentRect } from "./types.ts";

type LocatedThread = { thread_id: string; anchor: CommentAnchor };

export function createFrameLocator(frame: HTMLIFrameElement, changed: () => void) {
  const anchors = new Map<string, CommentAnchor>();
  const positions = new Map<string, CommentRect | null>();
  const request = (id: string, anchor: CommentAnchor | null, scroll = false) =>
    frame.contentWindow?.postMessage(
      { type: "rcp-artifact-locate", version: 1, id, anchor, scroll },
      "*",
    );
  return {
    track(threads: LocatedThread[]) {
      const current = new Set(threads.map((thread) => thread.thread_id));
      for (const id of anchors.keys()) {
        if (current.has(id)) continue;
        anchors.delete(id);
        positions.delete(id);
        request(id, null);
      }
      for (const { thread_id: id, anchor } of threads) {
        if (JSON.stringify(anchors.get(id)) === JSON.stringify(anchor)) continue;
        anchors.set(id, anchor);
        positions.delete(id);
        request(id, anchor);
      }
    },
    reload() {
      positions.clear();
      for (const [id, anchor] of anchors) request(id, anchor);
      changed();
    },
    locate(thread: LocatedThread) {
      request(thread.thread_id, thread.anchor, true);
    },
    receive(event: MessageEvent) {
      if (event.source !== frame.contentWindow) return;
      const value = event.data;
      if (
        !value ||
        value.type !== "rcp-artifact-position" ||
        value.version !== 1 ||
        !anchors.has(value.id)
      )
        return;
      const rect = value.rect;
      if (
        rect !== null &&
        (!rect ||
          ![rect.x, rect.y, rect.width, rect.height].every(Number.isFinite) ||
          rect.width < 0 ||
          rect.height < 0)
      )
        return;
      if (JSON.stringify(positions.get(value.id)) === JSON.stringify(rect)) return;
      positions.set(value.id, rect);
      changed();
    },
    position(id: string) {
      const rect = positions.get(id);
      if (!rect) return null;
      const bounds = frame.getBoundingClientRect();
      const x = rect.x + rect.width;
      const y = rect.y + rect.height;
      if (x < 0 || y < 0 || x > bounds.width || y > bounds.height) return null;
      return { x: bounds.x + x, y: bounds.y + y };
    },
  };
}
