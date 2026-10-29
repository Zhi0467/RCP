import { type ReactNode, useEffect, useRef, useState } from "react";
import { Download, LoaderCircle, RotateCcw } from "lucide-react";
import { getDocument, GlobalWorkerOptions, TextLayer } from "pdfjs-dist";
import type { PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { panelTools } from "./panelTools";
import type { PdfSpot, SyncTexMap } from "./synctex";
import { PaperBoxSelect, type PaperBox } from "./PaperBoxSelect";
import { registerPdfSynctex } from "./previewComments";

GlobalWorkerOptions.workerSrc = workerUrl;
type LoadedPdf = {
  pdf: PDFDocumentProxy;
  ratios: number[];
  /** Each page's width in PDF points, to place a SyncTeX y on its slot. */
  widths: number[];
  generation: number;
  destroy: () => void;
};

const scrollPositions = new Map<string, number>();
export default function PaperPdf({
  url,
  name = "paper.pdf",
  scrollKey = url,
  actions,
  toolbar,
  spot = null,
  path,
  synctex,
  onBox,
}: {
  url: string;
  name?: string;
  scrollKey?: string;
  actions?: ReactNode;
  toolbar: HTMLElement | null;
  /** Where the editor's cursor sits; the viewer scrolls there after each render and move. */
  spot?: PdfSpot | null;
  /** The file or compile root a selection in this PDF names. */
  path?: string;
  /** The compile's SyncTeX map, so a selection names the source line near it. */
  synctex?: SyncTexMap;
  /** A box drawn on a page; without it pages take no boxes. */
  onBox?: (box: PaperBox) => void;
}) {
  const viewer = useRef<HTMLDivElement>(null);
  const pages = useRef<HTMLDivElement>(null);
  const loadedTasks = useRef(new Map<() => void, number>());
  const nextGeneration = useRef(0);
  const visibleGeneration = useRef(0);
  const disposeVisible = useRef<(() => void) | null>(null);
  // Read only when the first document appears; a new key alone must not re-render pages.
  const restoreKey = useRef(scrollKey);
  restoreKey.current = scrollKey;
  const [width, setWidth] = useState(0);
  const [document, setDocument] = useState<LoadedPdf | null>(null);
  const [retry, setRetry] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const shownSlots = useRef<HTMLElement[]>([]);
  const [shown, setShown] = useState(0);
  useEffect(() => {
    const element = viewer.current;
    if (!element) return;
    registerPdfSynctex(element, synctex);
    return () => registerPdfSynctex(element, undefined);
  }, [synctex]);
  useEffect(() => {
    const container = pages.current;
    if (!container) return;
    // A hidden preview measures zero; keep its layout for when it is shown again.
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.floor(entry.contentRect.width);
      if (next) setWidth(next);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let disposed = false;
    let loaded = false;
    const generation = ++nextGeneration.current;
    setError(null);
    setLoading(true);
    const task = getDocument({ url, enableXfa: false });
    const destroy = () => {
      loadedTasks.current.delete(destroy);
      void task.destroy();
    };
    void (async () => {
      const pdf = await task.promise;
      const ratios: number[] = [];
      const widths: number[] = [];
      // Page dimensions reserve the complete scroll range without rasterizing it.
      for (let number = 1; number <= pdf.numPages; number++) {
        if (disposed) return;
        const page = await pdf.getPage(number);
        const natural = page.getViewport({ scale: 1 });
        ratios.push(natural.height / natural.width);
        widths.push(natural.width);
      }
      if (!disposed) {
        loaded = true;
        loadedTasks.current.set(destroy, generation);
        setDocument({ pdf, ratios, widths, generation, destroy });
      }
    })().catch((reason: unknown) => {
      destroy();
      if (!disposed) {
        setLoading(false);
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    });
    return () => {
      disposed = true;
      if (!loaded) destroy();
    };
  }, [url, retry]);
  useEffect(
    () => () => {
      disposeVisible.current?.();
      for (const destroy of loadedTasks.current.keys()) destroy();
    },
    [],
  );

  useEffect(() => {
    const container = pages.current;
    const root = viewer.current;
    if (!container || !root || !document || !width) return;
    let disposed = false;
    let committed = false;
    const jobs = new Map<HTMLElement, RenderTask>();
    const textJobs = new Map<HTMLElement, TextLayer>();
    const pending = new Map<HTMLElement, Promise<void>>();
    const wanted = new Set<HTMLElement>();
    const layer = window.document.createElement("div");
    const slots = document.ratios.map((ratio, index) => {
      const slot = window.document.createElement("div");
      slot.className = "paper-pdf-page";
      slot.style.height = `${width * ratio}px`;
      slot.dataset.page = String(index + 1);
      slot.dataset.points = String(document.widths[index]);
      slot.dataset.boxTarget = "";
      layer.append(slot);
      return slot;
    });
    const fail = (reason: unknown) => {
      if (disposed) return;
      if (visibleGeneration.current !== document.generation) {
        dispose();
        document.destroy();
      }
      setLoading(false);
      setError(reason instanceof Error ? reason.message : String(reason));
    };
    const render = (slot: HTMLElement): Promise<void> => {
      if (slot.firstChild) return Promise.resolve();
      const existing = pending.get(slot);
      if (existing) return existing;
      const work = (async () => {
        const page = await document.pdf.getPage(Number(slot.dataset.page));
        if (disposed || !wanted.has(slot)) return;
        const natural = page.getViewport({ scale: 1 });
        const scale = width / natural.width;
        const viewport = page.getViewport({
          scale: scale * Math.min(window.devicePixelRatio || 1, 2),
        });
        const canvas = window.document.createElement("canvas");
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        canvas.setAttribute("role", "img");
        canvas.setAttribute("aria-label", `PDF page ${slot.dataset.page}`);
        const job = page.render({ canvas, viewport });
        // Native selection over the pixels, laid out at the slot's CSS scale.
        const text = window.document.createElement("div");
        text.className = "textLayer";
        text.style.setProperty("--scale-factor", String(scale));
        const textJob = new TextLayer({
          textContentSource: page.streamTextContent(),
          container: text,
          viewport: page.getViewport({ scale }),
        });
        jobs.set(slot, job);
        textJobs.set(slot, textJob);
        try {
          await Promise.all([job.promise, textJob.render()]);
          if (!disposed && wanted.has(slot)) slot.replaceChildren(canvas, text);
        } catch (reason) {
          if (wanted.has(slot)) throw reason;
        } finally {
          jobs.delete(slot);
          textJobs.delete(slot);
        }
      })().finally(() => pending.delete(slot));
      pending.set(slot, work);
      return work;
    };
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const slot = entry.target as HTMLElement;
          if (entry.isIntersecting) {
            wanted.add(slot);
            void render(slot).catch(fail);
          } else {
            wanted.delete(slot);
            slot.replaceChildren();
          }
        }
      },
      { root, rootMargin: "100% 0px" },
    );
    void (async () => {
      // Stage the replacement offscreen. Keep the previous canvases (and scroll
      // range) until the pages around the current reading position are ready.
      let position: number;
      do {
        position = visibleGeneration.current
          ? root.scrollTop
          : (scrollPositions.get(restoreKey.current) ?? 0);
        let top = 0;
        const scroll = Math.min(
          position,
          Math.max(
            0,
            document.ratios.reduce((height, ratio) => height + width * ratio + 16, 0) -
              root.clientHeight,
          ),
        );
        wanted.clear();
        const near = slots.filter((slot) => {
          const height = Number.parseFloat(slot.style.height);
          const visible =
            top + height >= scroll - root.clientHeight && top <= scroll + 2 * root.clientHeight;
          top += height + 16;
          if (visible) wanted.add(slot);
          else slot.replaceChildren();
          return visible;
        });
        await Promise.all(near.map(render));
        if (disposed) return;
      } while (visibleGeneration.current && root.scrollTop !== position);
      disposeVisible.current?.();
      disposeVisible.current = dispose;
      committed = true;
      visibleGeneration.current = document.generation;
      // Pending URL loads own their own cleanup. Release the previous loaded
      // document only after its replacement has usable pixels.
      for (const [destroy, generation] of loadedTasks.current) {
        if (generation < document.generation) destroy();
      }
      container.replaceChildren(layer);
      root.scrollTop = position;
      shownSlots.current = slots;
      setShown(document.generation);
      slots.forEach((slot) => observer.observe(slot));
      setError(null);
      setLoading(false);
    })().catch(fail);
    function dispose() {
      disposed = true;
      observer.disconnect();
      wanted.clear();
      jobs.forEach((job) => job.cancel());
      textJobs.forEach((job) => job.cancel());
    }
    return () => {
      if (!committed) dispose();
    };
  }, [document, width]);

  useEffect(() => {
    const root = viewer.current;
    const slot = spot && shownSlots.current[spot.page - 1];
    if (!root || !slot || !document || !spot) return;
    const top =
      slot.getBoundingClientRect().top - root.getBoundingClientRect().top + root.scrollTop;
    const y = (spot.y * slot.clientWidth) / document.widths[spot.page - 1];
    // The cursor's line lands a third of the way down, with context above it.
    root.scrollTop = Math.max(0, top + y - root.clientHeight / 3);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- a spot moves the view; a new document only once shown
  }, [spot?.page, spot?.y, shown]);

  return (
    <div
      ref={viewer}
      data-paper-path={path}
      onScroll={(event) => {
        scrollPositions.set(scrollKey, event.currentTarget.scrollTop);
      }}
      className={`paper-pdf${error ? " paper-failed" : ""}`}
    >
      {panelTools(
        toolbar,
        <>
          {actions}
          {loading && <LoaderCircle size={16} className="spin" aria-label="Rendering PDF" />}
          {error && (
            <button
              className="icon-button"
              aria-label="Retry PDF"
              title={error}
              onClick={() => setRetry((value) => value + 1)}
            >
              <RotateCcw size={16} />
            </button>
          )}
          <a className="icon-button" href={url} download={name} aria-label="Download PDF">
            <Download size={16} />
          </a>
        </>,
      )}
      <PaperBoxSelect onBox={onBox}>
        <div className="paper-pdf-pages" ref={pages} />
      </PaperBoxSelect>
    </div>
  );
}
