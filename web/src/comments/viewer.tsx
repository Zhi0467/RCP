import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { ApiError } from "../core/api.ts";
import { createCommentsClient, fetchRequest } from "./api.ts";
import { findAnchor } from "./anchors.ts";
import { installBoxTool, type Point } from "./box.ts";
import { createFrameLocator } from "./viewerFrame.ts";
import { mountPdf } from "./pdf.ts";
import { selectionInput } from "./selectionInput.ts";
import { CommentComposer, type CommentOffer } from "./react/CommentComposer";
import { CommentPins } from "./react/CommentPins";
import { CommentRail } from "./react/CommentRail";
import { useCommentActions } from "./react/useCommentActions";
import { dockedThread, type DockThreads } from "./react/CommentDock";
import { sendKeys } from "./react/threadModel";
import type { CommentAnchor, CommentMember, CommentRect, CommentThread } from "./types.ts";

interface ViewerConfig {
  projectId: string;
  artifactId: string;
  selectionEnabled?: boolean;
  mediaType?: string;
  stateUrl: string;
  stateRefreshMs: number;
  maxSelections: number;
}
interface ViewerState {
  can_comment: boolean;
  origin_chat_id: string | null;
  fresh_session_required: boolean;
  edit_failure?: string | null;
}
interface Surface {
  /** Comment mode: while off, the page keeps its clicks. */
  select(on: boolean): void;
  locate(thread: CommentThread): Promise<void>;
  position(thread: CommentThread): Point | null;
  track(threads: CommentThread[]): void;
  destroy(): void;
}

const GONE = "The anchor is gone";

function displayedVersion(contentUrl: string): string | null {
  return new URL(contentUrl, location.href).searchParams.get("version_id");
}

function linkedThread() {
  return new URLSearchParams(location.hash.slice(1)).get("comment");
}

/** The shell's preview: the agent page's frame, an image, or PDF pages. Each
 *  locates and places anchors, and offers what a reader draws on it. */
function mountSurface(
  config: ViewerConfig,
  offer: (anchor: CommentAnchor, point?: Point, text?: string) => void,
  showError: (cause: unknown) => void,
): Surface {
  const abort = new AbortController();
  const frame = document.querySelector<HTMLIFrameElement>("#preview");
  const image = document.querySelector<HTMLImageElement>("#previewImage");
  const pdfRoot = document.querySelector<HTMLElement>("#previewPdf");
  const reposition = () => document.dispatchEvent(new Event("scroll"));
  const frameLocator = frame ? createFrameLocator(frame, reposition) : null;
  let disposed = false;
  let selecting = false;
  let locateRequest = 0;
  const imageBounds = (): CommentRect | undefined => {
    if (!image?.naturalWidth || !image.naturalHeight) return undefined;
    const bounds = image.getBoundingClientRect();
    const scale = Math.min(bounds.width / image.naturalWidth, bounds.height / image.naturalHeight);
    const width = image.naturalWidth * scale;
    const height = image.naturalHeight * scale;
    return {
      x: bounds.x + (bounds.width - width) / 2,
      y: bounds.y + (bounds.height - height) / 2,
      width,
      height,
    };
  };
  const pdf = pdfRoot?.dataset.contentUrl
    ? mountPdf(
        pdfRoot,
        pdfRoot.dataset.contentUrl,
        (anchor, point) => {
          if (config.selectionEnabled) offer(anchor, point);
        },
        showError,
      )
    : undefined;
  void pdf?.ready.then(reposition).catch(showError);
  const context = (thread: CommentThread) =>
    pdf
      ? {
          ...pdf.context(),
          sameVersion:
            !thread.drawn_version ||
            thread.drawn_version === displayedVersion(pdfRoot?.dataset.contentUrl || location.href),
        }
      : {
          text: "",
          element: () => null,
          textRect: () => null,
          image: imageBounds(),
          whole: imageBounds(),
        };
  const flash = (rect: CommentRect) => {
    const mark = document.createElement("div");
    mark.className = "rcp-comment-flash";
    Object.assign(mark.style, {
      left: `${rect.x}px`,
      top: `${rect.y}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
    });
    document.body.append(mark);
    mark.addEventListener("animationend", () => mark.remove(), { once: true });
    setTimeout(() => mark.remove(), 1200);
  };
  window.addEventListener(
    "message",
    (event) => {
      frameLocator?.receive(event);
      if (!selecting || !frame || event.source !== frame.contentWindow) return;
      const value: unknown = event.data;
      if (
        !value ||
        typeof value !== "object" ||
        !("type" in value) ||
        value.type !== "rcp-artifact-selection" ||
        !("version" in value) ||
        value.version !== 1 ||
        !("selection" in value)
      )
        return;
      const anchor = selectionInput(value.selection);
      if (anchor) offer(anchor);
    },
    { signal: abort.signal },
  );
  const postSelecting = () =>
    frame?.contentWindow?.postMessage(
      { type: selecting ? "rcp-artifact-selection-enable" : "rcp-artifact-selection-disable" },
      "*",
    );
  frame?.addEventListener(
    "load",
    () => {
      frameLocator?.reload();
      if (selecting) postSelecting();
    },
    { signal: abort.signal },
  );
  frameLocator?.reload();
  let clearBox: (() => void) | undefined;
  let observer: ResizeObserver | undefined;
  let layer: HTMLDivElement | undefined;
  if (image && config.selectionEnabled) {
    const drawLayer = document.createElement("div");
    layer = drawLayer;
    drawLayer.className = "rcp-comment-image-layer";
    drawLayer.hidden = true;
    document.body.append(drawLayer);
    const size = () => {
      const bounds = imageBounds();
      if (bounds)
        Object.assign(drawLayer.style, {
          left: `${bounds.x}px`,
          top: `${bounds.y}px`,
          width: `${bounds.width}px`,
          height: `${bounds.height}px`,
        });
    };
    observer = new ResizeObserver(size);
    observer.observe(image);
    image.addEventListener("load", size, { signal: abort.signal });
    window.addEventListener("scroll", size, { signal: abort.signal, capture: true });
    size();
    clearBox = installBoxTool(drawLayer, (rect) => {
      const bounds = imageBounds();
      offer(
        {
          kind: "box",
          rect,
          viewport: {
            width: Math.min(32768, image.naturalWidth),
            height: Math.min(32768, image.naturalHeight),
          },
          elements: [],
          comment: "",
        },
        {
          x: (bounds?.x ?? 0) + rect.x * (bounds?.width ?? 0),
          y: (bounds?.y ?? 0) + rect.y * (bounds?.height ?? 0),
        },
      );
    });
  }
  document
    .getElementById("askFix")
    ?.addEventListener(
      "click",
      () =>
        offer(
          { kind: "whole", comment: "" },
          undefined,
          document.getElementById("pageErrorMessage")?.textContent ?? "",
        ),
      { signal: abort.signal },
    );
  return {
    select(on) {
      selecting = Boolean(config.selectionEnabled) && on;
      postSelecting();
      if (layer) layer.hidden = !selecting;
    },
    track: (threads) => frameLocator?.track(threads),
    async locate(thread) {
      const request = ++locateRequest;
      if (frameLocator) {
        frameLocator.locate(thread);
        return;
      }
      if (pdf && thread.anchor.kind === "pdf") {
        try {
          await pdf.prepare(thread.anchor.page);
        } catch (error) {
          if (!disposed) showError(error);
          return;
        }
        if (disposed || request !== locateRequest) return;
      }
      const rect = findAnchor(thread.anchor, context(thread));
      if (!rect) return;
      if (pdf) pdf.scroll(rect);
      else image?.scrollIntoView({ block: "center" });
      const current = findAnchor(thread.anchor, context(thread));
      if (current) flash(current);
      reposition();
    },
    position(thread) {
      if (frameLocator) return frameLocator.position(thread.thread_id);
      const rect = findAnchor(thread.anchor, context(thread));
      if (!rect) return null;
      const point = { x: rect.x + rect.width, y: rect.y + rect.height };
      const bounds = pdfRoot?.getBoundingClientRect();
      if (
        bounds &&
        (point.y < bounds.top ||
          point.y > bounds.bottom ||
          point.x < bounds.left ||
          point.x > bounds.right)
      )
        return null;
      return point;
    },
    destroy() {
      disposed = true;
      abort.abort();
      clearBox?.();
      observer?.disconnect();
      layer?.remove();
      pdf?.destroy();
    },
  };
}

function ViewerComments({ config }: { config: ViewerConfig }) {
  const client = useMemo(
    () => createCommentsClient(config.projectId, fetchRequest()),
    [config.projectId],
  );
  const target = useMemo(
    () => ({ kind: "artifact" as const, id: config.artifactId }),
    [config.artifactId],
  );
  const [state, setState] = useState<ViewerState | null>(null);
  const [threads, setThreads] = useState<CommentThread[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [members, setMembers] = useState<CommentMember[]>([]);
  const [offer, setOffer] = useState<(CommentOffer & { key: number }) | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [selecting, setSelecting] = useState(false);
  const surface = useRef<Surface | null>(null);
  const revision = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;
  const freshRequired = useRef(false);
  const attempt = useRef<{ key: string; fresh: boolean } | null>(null);
  const canComment = Boolean(state?.can_comment);

  const loadThreads = useCallback(async () => {
    const request = ++revision.current;
    const page = await client.list(target);
    if (request !== revision.current) return null;
    surface.current?.track(page.threads);
    setThreads((current) =>
      JSON.stringify(current) === JSON.stringify(page.threads) ? current : page.threads,
    );
    setLoaded(true);
    return page.threads;
  }, [client, target]);
  const failRef = useRef<(cause: unknown) => void>(() => undefined);
  const reload = useCallback(() => {
    loadThreads().catch((cause: unknown) => failRef.current(cause));
  }, [loadThreads]);
  const send = useCallback(
    async (threadIds: string[], key: string) => {
      if (attempt.current?.key !== key)
        attempt.current = {
          key,
          fresh: freshRequired.current || Boolean(stateRef.current?.fresh_session_required),
        };
      try {
        const result = await client.sendArtifact(
          config.artifactId,
          threadIds,
          attempt.current.fresh,
          key,
        );
        freshRequired.current = false;
        window.parent.postMessage(
          {
            type: "rcp-artifact-edit-started",
            version: 1,
            artifact_id: config.artifactId,
            operation_id: result.operation_id,
          },
          location.origin,
        );
      } catch (error) {
        if (error instanceof ApiError && error.code === "fresh_session_required")
          freshRequired.current = true;
        throw error;
      }
    },
    [client, config.artifactId],
  );
  const keys = useRef(sendKeys());
  const dock = useCallback<DockThreads>(
    (saved) => {
      if (stateRef.current?.origin_chat_id) {
        window.parent.postMessage(
          {
            type: "rcp-artifact-comments-docked",
            version: 1,
            artifact_id: config.artifactId,
            threads: saved.map(dockedThread),
          },
          location.origin,
        );
        return;
      }
      const ids = saved.map((thread) => thread.thread_id);
      send(ids, keys.current.key(ids)).then(
        () => keys.current.done(),
        (cause: unknown) => {
          keys.current.failed(cause);
          failRef.current(cause);
        },
      );
    },
    [send, config.artifactId],
  );
  const comments = useCommentActions({
    client,
    threads,
    loaded,
    members,
    canPost: canComment,
    dock: canComment ? dock : null,
    reload,
  });
  failRef.current = comments.showError;
  const canPostRef = useRef(canComment);
  canPostRef.current = canComment;

  const locate = useCallback((thread: CommentThread) => {
    setSelected(thread.thread_id);
    void surface.current?.locate(thread);
  }, []);

  const offers = useRef(0);
  const offerAt = useCallback(
    (anchor: CommentAnchor, point?: Point, text?: string) => {
      if (!canPostRef.current) return;
      const frame = document.querySelector<HTMLIFrameElement>("#preview");
      const image = document.querySelector<HTMLImageElement>("#previewImage");
      const pdfUrl = document.querySelector<HTMLElement>("#previewPdf")?.dataset.contentUrl;
      setOffer({
        key: ++offers.current,
        target,
        anchor,
        drawnVersion: displayedVersion(frame?.src || image?.src || pdfUrl || location.href),
        point: point ?? null,
        text,
      });
    },
    [target],
  );

  useEffect(() => {
    surface.current = mountSurface(config, offerAt, (cause) => failRef.current(cause));
    return () => {
      surface.current?.destroy();
      surface.current = null;
    };
  }, [config, offerAt]);

  const hasMode = Boolean(config.selectionEnabled) && config.mediaType !== "application/pdf";
  const commentMode = hasMode && canComment && selecting;
  useEffect(() => {
    surface.current?.select(commentMode);
    if (!commentMode) setOffer((current) => (current?.anchor.kind === "whole" ? current : null));
  }, [commentMode]);

  useEffect(() => {
    if (!canComment) setOffer(null);
  }, [canComment]);

  useEffect(() => {
    const abort = new AbortController();
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let membersAsked = false;
    let pending = linkedThread();
    const refresh = async () => {
      try {
        const response = await fetch(config.stateUrl, {
          credentials: "same-origin",
          signal: abort.signal,
        });
        if (!response.ok) throw new Error("Comment availability could not be loaded");
        const next = (await response.json()) as ViewerState;
        if (disposed) return;
        setState((current) => (JSON.stringify(current) === JSON.stringify(next) ? current : next));
        if (next.can_comment && !membersAsked) {
          membersAsked = true;
          client.members().then(
            (list) => {
              if (!disposed) setMembers(list);
            },
            () => {
              membersAsked = false;
            },
          );
        }
        const loadedThreads = await loadThreads();
        if (disposed) return;
        const linked = pending && loadedThreads?.find((thread) => thread.thread_id === pending);
        if (linked) {
          pending = null;
          locate(linked);
        }
        if (next.edit_failure)
          failRef.current(`The last edit did not finish: ${next.edit_failure}`);
      } catch (error) {
        if (disposed) return;
        setState(null);
        failRef.current(error);
      }
    };
    const poll = async () => {
      if (!document.hidden) await refresh();
      if (!disposed) timer = setTimeout(poll, config.stateRefreshMs);
    };
    void poll();
    window.addEventListener("focus", () => void refresh(), { signal: abort.signal });
    window.addEventListener(
      "hashchange",
      () => {
        pending = linkedThread();
        void refresh();
      },
      { signal: abort.signal },
    );
    return () => {
      disposed = true;
      clearTimeout(timer);
      abort.abort();
    };
  }, [config, client, loadThreads, locate]);

  const position = (thread: CommentThread) => surface.current?.position(thread) ?? null;
  return (
    <>
      <CommentRail
        comments={comments}
        wholeLabel="Whole artifact"
        selected={selected}
        onLocate={locate}
        note={(thread) =>
          thread.state === "open" && thread.anchor.kind !== "whole" && !position(thread)
            ? GONE
            : undefined
        }
        onWhole={() => offerAt({ kind: "whole", comment: "" })}
        commentMode={
          hasMode && canComment
            ? { on: commentMode, toggle: () => setSelecting(!commentMode) }
            : undefined
        }
      />
      <CommentPins
        threads={comments.threads}
        position={position}
        selected={selected}
        onPick={locate}
      />
      {offer && (
        <CommentComposer
          key={offer.key}
          offer={offer}
          comments={comments}
          onClose={() => setOffer(null)}
          onCreated={(thread) => setSelected(thread.thread_id)}
        />
      )}
    </>
  );
}

export function mountViewer(config: ViewerConfig) {
  const element = document.getElementById("comments-root");
  if (!element) throw new Error("Comment mount is missing");
  const root = createRoot(element);
  root.render(<ViewerComments config={config} />);
  window.addEventListener("pagehide", () => root.unmount(), { once: true });
}
