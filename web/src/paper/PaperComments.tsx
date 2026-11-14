import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { EditorView } from "@codemirror/view";
import { findAnchor, findText, type AnchorContext } from "../comments/anchors.ts";
import type { Point } from "../comments/box.ts";
import type { CommentRect, CommentThread } from "../comments/types.ts";
import { drawnInPreview, linesWhere } from "../comments/where.ts";
import { CommentComposer, type CommentOffer } from "../comments/react/CommentComposer";
import { CommentPins } from "../comments/react/CommentPins";
import { CommentRail } from "../comments/react/CommentRail";
import {
  flashRect,
  insideElement,
  rangeEnd,
  rectEnd,
  textRange,
  toRect,
} from "../comments/react/domText.ts";
import { MAX_QUOTE_CHARS, surroundingText } from "../comments/react/threadModel.ts";
import { useComments, type SendThreads } from "../comments/react/useComments";
import {
  SelectionCommentButton,
  useSelectionComment,
  type SelectionCommentOffer,
} from "../comments/react/useSelectionComment";
import { takeCommentThreadRequest, useCommentThreadRequest } from "../core/commentThreadRequest";
import type { PaperFile } from "../core/types";
import { previewSelectionAnchor } from "./previewAnchors";
import type { PreviewAnchor } from "./PaperPreview";
import "./paper-comments.css";

const GONE = "The anchor is gone";
/** A view just switched to may still be laying out or rendering its pages. */
const LOCATE_RETRIES = [0, 120, 400, 900];

function sourceView(pane: HTMLElement | null): EditorView | null {
  const element = pane?.querySelector<HTMLElement>(".cm-editor");
  return (element && EditorView.findFromDOM(element)) ?? null;
}

/** A source thread's place in the editor: the quote's range, re-found by its text. */
function sourceContext(view: EditorView): AnchorContext {
  return {
    text: view.state.doc.toString(),
    element: () => null,
    textRect: (start, end) => {
      const from = view.coordsAtPos(start);
      const to = view.coordsAtPos(end, -1);
      if (!from || !to) return null;
      return {
        x: Math.min(from.left, to.left),
        y: from.top,
        width: Math.max(Math.abs(to.right - from.left), 2),
        height: Math.max(to.bottom - from.top, 2),
      };
    },
  };
}

function sourceEnd(view: EditorView, thread: CommentThread): Point | null {
  if (thread.anchor.kind !== "text") return null;
  const match = findText(
    view.state.doc.toString(),
    thread.anchor.text,
    thread.anchor.surrounding_text,
  );
  const coords = match && view.coordsAtPos(match.end, -1);
  return coords ? { x: coords.right, y: (coords.top + coords.bottom) / 2 } : null;
}

/** The preview's places, read from its DOM: rendered text, PDF page slots, an image. */
function previewContext(pane: HTMLElement, sameVersion: boolean): AnchorContext {
  const host = pane.querySelector<HTMLElement>("article[data-paper-path], pre[data-paper-path]");
  const image = pane.querySelector<HTMLImageElement>(".paper-image-frame img");
  const slot = (page: number) =>
    pane.querySelector<HTMLElement>(`.paper-pdf-page[data-page="${page}"]`);
  return {
    text: host?.textContent ?? "",
    element: () => null,
    image: image ? toRect(image.getBoundingClientRect()) : undefined,
    sameVersion,
    page: (page) => {
      const element = slot(page);
      if (!element) return null;
      return {
        bounds: toRect(element.getBoundingClientRect()),
        text: element.querySelector(".textLayer")?.textContent ?? "",
      };
    },
    textRect: (start, end, page) => {
      const root = page === undefined ? host : slot(page)?.querySelector<HTMLElement>(".textLayer");
      const range = root && textRange(root, start, end);
      return range ? toRect(range.getBoundingClientRect()) : null;
    },
  };
}

function previewEnd(pane: HTMLElement, thread: CommentThread, sameVersion: boolean) {
  const { anchor } = thread;
  if (anchor.kind === "text") {
    const host = pane.querySelector<HTMLElement>("article[data-paper-path], pre[data-paper-path]");
    const match = host && findText(host.textContent ?? "", anchor.text, anchor.surrounding_text);
    const range = match && textRange(host, match.start, match.end);
    return range ? rangeEnd(range) : null;
  }
  const rect = findAnchor(anchor, previewContext(pane, sameVersion));
  return rect && rectEnd(rect);
}

/** Moves `container` so `rect` sits a third of the way down it. */
function scrollWithin(container: Element | null, rect: CommentRect) {
  if (!container) return;
  const box = container.getBoundingClientRect();
  container.scrollBy({
    top: rect.y - box.top - box.height / 3,
    behavior: "smooth",
  });
}

/** Paper's shared comments: the rail for the open file, pins in the editor and the
 *  preview, Comment on selections in either, and the composer. Threads go to the
 *  Coach through its own sender. */
export function usePaperComments({
  projectId,
  file,
  subscribe,
  editorPane,
  previewPane,
  sourceVisible,
  previewVisible,
  showSource,
  showPreview,
  send,
  canPost,
}: {
  projectId: string;
  file: PaperFile | undefined;
  /** Calls back when the open file's text changes, which moves its pins. */
  subscribe?: (listener: () => void) => () => void;
  editorPane: RefObject<HTMLElement | null>;
  previewPane: RefObject<HTMLElement | null>;
  sourceVisible: boolean;
  previewVisible: boolean;
  showSource: () => void;
  showPreview: () => void;
  send: SendThreads | null;
  canPost: boolean;
}) {
  const fileId = file?.file_id ?? null;
  const ids = useMemo(() => (fileId ? [fileId] : []), [fileId]);
  const [railOpen, setRailOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [offer, setOffer] = useState<CommentOffer | null>(null);
  const comments = useComments({
    projectId,
    kind: "paper",
    ids,
    canPost,
    send,
    picker: railOpen || Boolean(offer),
  });
  const [frameOffer, setFrameOffer] = useState<PreviewAnchor | null>(null);
  const version = file?.sha256 ?? null;
  const target = useMemo(() => (fileId ? { kind: "paper" as const, id: fileId } : null), [fileId]);

  const shownOffer = useRef<CommentOffer | null>(null);
  /** Shows `next` in the composer; the mark of the one it replaces goes. */
  const showOffer = useCallback((next: CommentOffer | null) => {
    if (shownOffer.current !== next) shownOffer.current?.clear?.();
    shownOffer.current = next;
    setOffer(next);
  }, []);
  const closeOffer = useCallback(() => showOffer(null), [showOffer]);
  const attempt = useRef(0);
  useEffect(() => {
    closeOffer();
    setFrameOffer(null);
    setSelected(null);
    attempt.current += 1;
  }, [fileId, closeOffer]);

  const open = (
    anchor: CommentOffer["anchor"],
    rect: PreviewAnchor["rect"],
    clear?: () => void,
  ) => {
    if (!target || !canPost) {
      clear?.();
      return;
    }
    showOffer({
      target,
      anchor,
      drawnVersion: version,
      point: { x: rect.left, y: rect.bottom },
      clear,
    });
  };

  const resolveSelection = (selection: Selection): SelectionCommentOffer | null => {
    if (!file) return null;
    const node = selection.anchorNode;
    const editor = editorPane.current;
    if (editor && node && editor.contains(node)) {
      const view = sourceView(editor);
      const range = view?.state.selection.main;
      if (!view || !range || range.empty) return null;
      const raw = view.state.sliceDoc(range.from, range.to);
      const lead = raw.length - raw.trimStart().length;
      const quote = raw.trim().slice(0, MAX_QUOTE_CHARS);
      if (!quote) return null;
      const from = range.from + lead;
      const to = from + quote.length;
      const doc = view.state.doc;
      const coords = view.coordsAtPos(to, -1);
      if (!coords) return null;
      const anchor = {
        kind: "text" as const,
        text: quote,
        surrounding_text: surroundingText(doc.toString(), from, to),
        comment: "",
        where: linesWhere(file.path, doc.lineAt(from).number, doc.lineAt(to).number),
      };
      return { rect: coords, open: () => open(anchor, coords) };
    }
    const pane = previewPane.current;
    if (!pane || !node || !pane.contains(node)) return null;
    const drawn = previewSelectionAnchor(selection, pane);
    return drawn && { rect: drawn.rect, open: () => open(drawn.anchor, drawn.rect) };
  };
  const selectionOffer = useSelectionComment(canPost && !offer && Boolean(file), resolveSelection);

  const visibleIn = (thread: CommentThread) =>
    drawnInPreview(thread.anchor) ? previewVisible : sourceVisible;
  const sameVersion = (thread: CommentThread) => thread.drawn_version === version;
  /** Where the thread's anchor ends in its view, scrolled out of sight or not. */
  const placed = (thread: CommentThread): { point: Point; frame: Element } | null => {
    if (drawnInPreview(thread.anchor)) {
      const pane = previewPane.current;
      const point = pane && previewEnd(pane, thread, sameVersion(thread));
      return point ? { point, frame: pane } : null;
    }
    const view = sourceView(editorPane.current);
    const point = view && sourceEnd(view, thread);
    return point ? { point, frame: view.scrollDOM } : null;
  };
  const position = (thread: CommentThread): Point | null => {
    const found = visibleIn(thread) ? placed(thread) : null;
    return found && insideElement(found.point, found.frame) ? found.point : null;
  };
  /** Scrolls the thread's place into view and flashes it; false when it is not found. */
  const locateNow = (thread: CommentThread): boolean => {
    if (drawnInPreview(thread.anchor)) {
      const pane = previewPane.current;
      if (!pane) return false;
      const rect = findAnchor(thread.anchor, previewContext(pane, sameVersion(thread)));
      if (!rect) return false;
      scrollWithin(pane.querySelector(".paper-pdf") ?? pane, rect);
      requestAnimationFrame(() => {
        const moved = findAnchor(thread.anchor, previewContext(pane, sameVersion(thread)));
        if (moved) flashRect(moved);
      });
      return true;
    }
    const view = sourceView(editorPane.current);
    if (!view || thread.anchor.kind !== "text") return false;
    const match = findText(
      view.state.doc.toString(),
      thread.anchor.text,
      thread.anchor.surrounding_text,
    );
    if (!match) return false;
    view.dispatch({ effects: EditorView.scrollIntoView(match.start, { y: "center" }) });
    requestAnimationFrame(() => {
      const rect = findAnchor(thread.anchor, sourceContext(view));
      if (rect) flashRect(rect);
    });
    return true;
  };
  const locate = (thread: CommentThread) => {
    setSelected(thread.thread_id);
    if (thread.anchor.kind === "whole") return;
    if (drawnInPreview(thread.anchor)) {
      if (!previewVisible) showPreview();
    } else if (!sourceVisible) showSource();
    const current = ++attempt.current;
    const timers = LOCATE_RETRIES.map((delay) =>
      window.setTimeout(() => {
        if (attempt.current !== current) return;
        if (locateNow(thread)) {
          attempt.current += 1;
          timers.forEach(clearTimeout);
        }
      }, delay),
    );
  };
  const locateRef = useRef(locate);
  locateRef.current = locate;

  const request = useCommentThreadRequest();
  useEffect(() => {
    if (!fileId || !comments.loaded) return;
    const threadId = takeCommentThreadRequest(projectId, "paper", fileId);
    if (!threadId) return;
    setRailOpen(true);
    setSelected(threadId);
    const thread = comments.threads.find((item) => item.thread_id === threadId);
    if (thread) locateRef.current(thread);
  }, [request, projectId, fileId, comments.loaded, comments.threads]);

  const note = (thread: CommentThread) =>
    thread.anchor.kind !== "whole" &&
    thread.state === "open" &&
    visibleIn(thread) &&
    !(file?.format === "html" && drawnInPreview(thread.anchor)) &&
    !placed(thread)
      ? GONE
      : undefined;

  const openCount = comments.threads.filter((thread) => thread.state === "open").length;
  const layers = (
    <>
      {railOpen && file && (
        <CommentRail
          className="paper-comment-rail"
          comments={comments}
          agentLabel="Coach"
          wholeLabel="Whole file"
          selected={selected}
          onLocate={locate}
          note={note}
          onWhole={() =>
            open(
              { kind: "whole", comment: "", where: file.path },
              {
                left: window.innerWidth,
                right: window.innerWidth,
                top: 72,
                bottom: 72,
              },
            )
          }
          onClose={() => setRailOpen(false)}
        />
      )}
      <CommentPins
        threads={comments.threads}
        position={position}
        selected={selected}
        revision={`${fileId}:${sourceVisible}:${previewVisible}`}
        subscribe={subscribe}
        onPick={(thread) => {
          setRailOpen(true);
          setSelected(thread.thread_id);
        }}
      />
      <SelectionCommentButton
        offer={selectionOffer.offer}
        onOpen={() => selectionOffer.setOffer(null)}
      />
      {frameOffer && !offer && (
        <SelectionCommentButton
          offer={{
            rect: frameOffer.rect,
            open: () => open(frameOffer.anchor, frameOffer.rect, frameOffer.clear),
          }}
          onOpen={() => setFrameOffer(null)}
        />
      )}
      {offer && (
        <CommentComposer
          offer={offer}
          comments={comments}
          agentLabel="Coach"
          canSend
          onClose={closeOffer}
          onCreated={(thread) => {
            setRailOpen(true);
            setSelected(thread.thread_id);
          }}
        />
      )}
    </>
  );
  return {
    openCount,
    railOpen,
    setRailOpen,
    /** A box or frame selection drawn in the preview opens the composer on it. */
    openAnchor: (drawn: PreviewAnchor) => open(drawn.anchor, drawn.rect, drawn.clear),
    /** Text selected inside an HTML page offers Comment, or withdraws the offer. */
    offerFrameSelection: setFrameOffer,
    /** The frame cancelled the selection drawn with `clear`. */
    cancelAnchor: (clear: () => void) => {
      if (shownOffer.current?.clear !== clear) return;
      shownOffer.current = null;
      setOffer(null);
    },
    layers,
  };
}
