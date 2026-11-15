import { getDocument, GlobalWorkerOptions, TextLayer } from "pdfjs-dist";
import type { RenderTask } from "pdfjs-dist";
import workerSource from "pdfjs-dist/build/pdf.worker.min.mjs?raw";
import { boxFractions, installBoxTool } from "./box.ts";
import type { AnchorContext } from "./anchors.ts";
import type { CommentAnchor, CommentRect } from "./types.ts";
import "./pdf.css";

export function mountPdf(
  root: HTMLElement,
  url: string,
  onAnchor: (anchor: CommentAnchor, point?: { x: number; y: number }) => void,
  onError: (error: unknown) => void,
) {
  const workerUrl = URL.createObjectURL(new Blob([workerSource], { type: "text/javascript" }));
  const taskPromise = Promise.resolve().then(() => {
    GlobalWorkerOptions.workerSrc = workerUrl;
    return getDocument({ url, enableXfa: false });
  });
  const abort = new AbortController();
  const jobs = new Set<RenderTask>();
  const textJobs = new Set<TextLayer>();
  const cleanup: (() => void)[] = [];
  const slots: HTMLElement[] = [];
  let preparePage: (number: number) => Promise<void> = async () => {};
  let disposed = false;
  let area = false;
  root.classList.add("rcp-pdf");
  const toolbar = document.createElement("div");
  toolbar.className = "rcp-pdf-toolbar";
  const selectArea = document.createElement("button");
  selectArea.type = "button";
  selectArea.textContent = "Select area";
  selectArea.setAttribute("aria-pressed", "false");
  selectArea.addEventListener(
    "click",
    () => {
      area = !area;
      root.classList.toggle("rcp-pdf-area", area);
      selectArea.setAttribute("aria-pressed", String(area));
      document.getSelection()?.removeAllRanges();
    },
    { signal: abort.signal },
  );
  toolbar.append(selectArea);
  const pages = document.createElement("div");
  pages.className = "rcp-pdf-pages";
  root.append(toolbar, pages);
  root.addEventListener(
    "mouseup",
    () => {
      if (area) return;
      const selection = document.getSelection();
      if (!selection?.rangeCount || selection.isCollapsed) return;
      const selectedText = selection.toString().trim().slice(0, 4096);
      if (!selectedText) return;
      const range = selection.getRangeAt(0);
      const slot = slots.find(
        (candidate) =>
          candidate.contains(range.startContainer) && candidate.contains(range.endContainer),
      );
      if (!slot) return;
      const bounds = range.getBoundingClientRect();
      const rect = boxFractions(
        { x: bounds.left, y: bounds.top },
        { x: bounds.right, y: bounds.bottom },
        slot.getBoundingClientRect(),
      );
      if (rect)
        onAnchor(
          {
            kind: "pdf",
            page: Number(slot.dataset.page),
            rect,
            text: selectedText,
            comment: "",
          },
          { x: bounds.right, y: bounds.bottom },
        );
    },
    { signal: abort.signal },
  );
  const ready = (async () => {
    const task = await taskPromise;
    const pdf = await task.promise;
    const wanted = new Set<HTMLElement>();
    const pending = new Map<HTMLElement, Promise<void>>();
    const render = (slot: HTMLElement, force = false): Promise<void> => {
      if (slot.querySelector("canvas")) return Promise.resolve();
      const existing = pending.get(slot);
      if (existing) return force ? existing.then(() => render(slot, true)) : existing;
      const work = (async () => {
        const number = Number(slot.dataset.page);
        const page = await pdf.getPage(number);
        if (disposed || (!force && !wanted.has(slot))) return;
        const natural = page.getViewport({ scale: 1 });
        const width = Math.max(1, pages.clientWidth);
        const scale = width / natural.width;
        const viewport = page.getViewport({ scale: scale * Math.min(devicePixelRatio || 1, 2) });
        const canvas = document.createElement("canvas");
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        canvas.setAttribute("role", "img");
        canvas.setAttribute("aria-label", `PDF page ${number}`);
        const text = document.createElement("div");
        text.className = "textLayer";
        text.style.setProperty("--scale-factor", String(scale));
        const layer = new TextLayer({
          textContentSource: page.streamTextContent(),
          container: text,
          viewport: page.getViewport({ scale }),
        });
        const job = page.render({ canvas, viewport });
        jobs.add(job);
        textJobs.add(layer);
        try {
          await Promise.all([job.promise, layer.render()]);
        } finally {
          jobs.delete(job);
          textJobs.delete(layer);
        }
        if (disposed || (!force && !wanted.has(slot))) return;
        const content = slot.querySelector<HTMLElement>(".rcp-pdf-content")!;
        content.style.width = `${width}px`;
        content.style.height = `${natural.height * scale}px`;
        content.dataset.width = String(width);
        content.style.transform = `scale(${slot.clientWidth / width})`;
        content.replaceChildren(canvas, text);
      })().finally(() => pending.delete(slot));
      pending.set(slot, work);
      return work;
    };
    preparePage = async (number) => {
      const slot = slots[number - 1];
      if (!slot) return;
      await render(slot, true);
    };
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const slot = entry.target as HTMLElement;
          if (entry.isIntersecting) {
            wanted.add(slot);
            void render(slot).catch((error: unknown) => {
              if (!disposed) onError(error);
            });
          } else {
            wanted.delete(slot);
            const canvas = slot.querySelector("canvas");
            if (canvas) {
              canvas.width = 0;
              canvas.height = 0;
              canvas.remove();
            }
          }
        }
      },
      { root, rootMargin: "600px 0px" },
    );
    cleanup.push(() => observer.disconnect());
    for (let number = 1; number <= pdf.numPages && !disposed; number++) {
      const page = await pdf.getPage(number);
      if (disposed) return;
      const natural = page.getViewport({ scale: 1 });
      const slot = document.createElement("div");
      slot.className = "rcp-pdf-page";
      slot.dataset.page = String(number);
      slot.style.aspectRatio = `${natural.width} / ${natural.height}`;
      const content = document.createElement("div");
      content.className = "rcp-pdf-content";
      const box = document.createElement("div");
      box.className = "rcp-pdf-box";
      slot.append(content, box);
      slots.push(slot);
      pages.append(slot);
      cleanup.push(
        installBoxTool(box, (rect) => {
          const bounds = slot.getBoundingClientRect();
          onAnchor(
            { kind: "pdf", page: number, rect, text: "", comment: "" },
            {
              x: bounds.x + (rect.x + rect.width) * bounds.width,
              y: bounds.y + (rect.y + rect.height) * bounds.height,
            },
          );
        }),
      );
      const resize = new ResizeObserver(() => {
        const width = Number(content.dataset.width);
        if (width) content.style.transform = `scale(${slot.clientWidth / width})`;
      });
      resize.observe(slot);
      cleanup.push(() => resize.disconnect());
      observer.observe(slot);
    }
    if (!disposed && slots[0]) {
      wanted.add(slots[0]);
      await render(slots[0]);
    }
  })().catch((error: unknown) => {
    if (!disposed) onError(error);
  });
  const context = (): AnchorContext => ({
    text: "",
    element: () => null,
    textRect: (start, end, number) => {
      const text = number && slots[number - 1]?.querySelector(".textLayer");
      if (!text) return null;
      const nodes = document.createTreeWalker(text, NodeFilter.SHOW_TEXT);
      const range = document.createRange();
      let offset = 0;
      let found = false;
      for (let node = nodes.nextNode(); node; node = nodes.nextNode()) {
        const length = node.textContent?.length ?? 0;
        if (!found && start < offset + length) {
          range.setStart(node, start - offset);
          found = true;
        }
        if (found && end <= offset + length) {
          range.setEnd(node, end - offset);
          return range.getBoundingClientRect();
        }
        offset += length;
      }
      return null;
    },
    whole: root.getBoundingClientRect(),
    page: (number) => {
      const slot = slots[number - 1];
      return slot
        ? {
            bounds: slot.getBoundingClientRect(),
            text: slot.querySelector(".textLayer")?.textContent ?? "",
          }
        : null;
    },
  });
  return {
    ready,
    prepare: async (number: number) => {
      await ready;
      await preparePage(number);
    },
    context,
    scroll: (rect: CommentRect) => {
      root.scrollTop += rect.y - root.getBoundingClientRect().y - root.clientHeight / 3;
    },
    destroy: () => {
      disposed = true;
      abort.abort();
      jobs.forEach((job) => job.cancel());
      textJobs.forEach((job) => job.cancel());
      cleanup.forEach((clear) => clear());
      void taskPromise
        .then((task) => task.destroy())
        .finally(() => URL.revokeObjectURL(workerUrl))
        .catch(() => {});
      root.replaceChildren();
    },
  };
}
