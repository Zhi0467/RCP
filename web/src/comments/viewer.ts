import { createCommentsClient, CommentRequestError } from "./api.ts";
import { findAnchor } from "./anchors.ts";
import { installBoxTool } from "./box.ts";
import { createFrameLocator } from "./viewerFrame.ts";
import { mountPdf } from "./pdf.ts";
import { displayedVersion } from "./viewerModel.ts";
import { selectionInput } from "./selectionInput.ts";
import { mountComments } from "./ui.ts";
import type { CommentRect, CommentThread } from "./types.ts";
export { mountComments } from "./ui.ts";

interface ViewerConfig {
  projectId: string;
  artifactId: string;
  mediaType?: string;
  selectionEnabled?: boolean;
  stateUrl: string;
  stateRefreshMs: number;
  maxSelections: number;
}
interface ViewerState {
  can_comment: boolean;
  fresh_session_required: boolean;
  edit_failure?: string | null;
}

export function mountViewer(config: ViewerConfig) {
  const root = document.getElementById("comments-root");
  if (!root) throw new Error("Comment mount is missing");
  const client = createCommentsClient(config.projectId);
  const target = { kind: "artifact" as const, id: config.artifactId };
  const frame = document.querySelector<HTMLIFrameElement>("#preview");
  const image = document.querySelector<HTMLImageElement>("#previewImage");
  const abort = new AbortController();
  const reposition = () => document.dispatchEvent(new Event("scroll"));
  const frameLocator = frame ? createFrameLocator(frame, reposition) : null;
  const pdfRoot = document.querySelector<HTMLElement>("#previewPdf");
  let pdf: ReturnType<typeof mountPdf> | undefined;
  let state: ViewerState | null = null;
  let disposed = false;
  let locateRequest = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let sendAttempt: { ids: string; key: string; freshSession: boolean } | null = null;
  let freshSessionRequired = false;
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
  const context = (thread?: CommentThread) =>
    pdf
      ? {
          ...pdf.context(),
          sameVersion:
            !thread?.drawn_version ||
            thread.drawn_version ===
              displayedVersion(pdfRoot?.dataset.contentUrl || location.href, location.href),
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
  const ui = mountComments(root, {
    canPost: false,
    canSend: false,
    members: [],
    maxSelections: config.maxSelections,
    load: async () => {
      const page = await client.list(target);
      frameLocator?.track(page.threads);
      if (!disposed)
        ui.update({
          canPost: Boolean(state?.can_comment),
          canSend: Boolean(state?.can_comment),
        });
      return page.threads;
    },
    create: (anchor, text) =>
      client.create(
        target,
        anchor,
        text,
        displayedVersion(
          frame?.src || image?.src || pdfRoot?.dataset.contentUrl || location.href,
          location.href,
        ),
      ),
    reply: (id, text) => client.reply(id, text),
    resolve: (id, resolved) => (resolved ? client.resolve(id) : client.reopen(id)),
    send: async (ids) => {
      const signature = JSON.stringify(ids);
      if (sendAttempt?.ids !== signature)
        sendAttempt = {
          ids: signature,
          key: crypto.randomUUID(),
          freshSession: freshSessionRequired || Boolean(state?.fresh_session_required),
        };
      try {
        const result = await client.sendArtifact(
          config.artifactId,
          ids,
          sendAttempt.freshSession,
          sendAttempt.key,
        );
        sendAttempt = null;
        freshSessionRequired = false;
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
        if (
          error instanceof CommentRequestError &&
          error.status === 409 &&
          typeof error.detail === "object" &&
          error.detail &&
          "code" in error.detail &&
          error.detail.code === "fresh_session_required"
        ) {
          freshSessionRequired = true;
          sendAttempt = null;
        }
        throw error;
      }
    },
    locate: async (thread: CommentThread) => {
      const request = ++locateRequest;
      if (frameLocator) {
        frameLocator.locate(thread);
        return;
      }
      if (pdf && thread.anchor.kind === "pdf") {
        try {
          await pdf.prepare(thread.anchor.page);
        } catch (error) {
          if (!disposed) ui.showError(error);
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
    position: (thread) => {
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
  });
  if (pdfRoot?.dataset.contentUrl) {
    pdf = mountPdf(
      pdfRoot,
      pdfRoot.dataset.contentUrl,
      (anchor, point) => {
        if (config.selectionEnabled) ui.offer(anchor, point);
      },
      (error) => ui.showError(error),
    );
    void pdf.ready.then(reposition).catch((error: unknown) => ui.showError(error));
  }
  root.querySelector(".rcp-comments-notice")?.setAttribute("id", "notice");
  window.addEventListener(
    "message",
    (event) => {
      frameLocator?.receive(event);
      if (!config.selectionEnabled || !frame || event.source !== frame.contentWindow) return;
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
      ui.offer(selectionInput(value.selection));
    },
    { signal: abort.signal },
  );
  const enableSelection = () => {
    frameLocator?.reload();
    if (config.selectionEnabled)
      frame?.contentWindow?.postMessage({ type: "rcp-artifact-selection-enable" }, "*");
  };
  frame?.addEventListener("load", enableSelection, { signal: abort.signal });
  enableSelection();
  let clearBox: (() => void) | undefined;
  let observer: ResizeObserver | undefined;
  let layer: HTMLDivElement | undefined;
  if (image && config.selectionEnabled) {
    layer = document.createElement("div");
    layer.className = "rcp-comment-image-layer";
    document.body.append(layer);
    const size = () => {
      const bounds = imageBounds();
      if (bounds && layer)
        Object.assign(layer.style, {
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
    clearBox = installBoxTool(layer, (rect) =>
      ui.offer(
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
          x: (imageBounds()?.x ?? 0) + rect.x * (imageBounds()?.width ?? 0),
          y: (imageBounds()?.y ?? 0) + rect.y * (imageBounds()?.height ?? 0),
        },
      ),
    );
  }
  document
    .getElementById("askFix")
    ?.addEventListener(
      "click",
      () =>
        ui.offer(
          { kind: "whole", comment: "" },
          undefined,
          document.getElementById("pageErrorMessage")?.textContent ?? "",
        ),
      { signal: abort.signal },
    );
  const refresh = async () => {
    if (disposed) return;
    try {
      const response = await fetch(config.stateUrl, {
        credentials: "same-origin",
        signal: abort.signal,
      });
      if (!response.ok) throw new Error("Comment availability could not be loaded");
      state = (await response.json()) as ViewerState;
      await ui.refresh();
      if (state.edit_failure) ui.showError(`The last edit did not finish: ${state.edit_failure}`);
    } catch (error) {
      if (!disposed) {
        state = null;
        ui.update({ canSend: false });
        ui.showError(error);
      }
    }
  };
  const poll = async () => {
    if (!document.hidden) await refresh();
    if (!disposed) timer = setTimeout(poll, config.stateRefreshMs);
  };
  void poll();
  window.addEventListener("focus", () => void refresh(), { signal: abort.signal });
  const destroy = () => {
    disposed = true;
    clearTimeout(timer);
    abort.abort();
    clearBox?.();
    observer?.disconnect();
    layer?.remove();
    pdf?.destroy();
    ui.destroy();
  };
  window.addEventListener("pagehide", destroy, { once: true, signal: abort.signal });
  return { ...ui, destroy };
}
