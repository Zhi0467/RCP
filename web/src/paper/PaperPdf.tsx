import { useEffect, useRef, useState } from "react";
import { Download, LoaderCircle, RotateCcw } from "lucide-react";
import { getDocument, GlobalWorkerOptions } from "pdfjs-dist";
import type { PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

GlobalWorkerOptions.workerSrc = workerUrl;
type LoadedPdf = {
  pdf: PDFDocumentProxy;
  ratios: number[];
  generation: number;
  destroy: () => void;
};

export default function PaperPdf({ url, name = "paper.pdf" }: { url: string; name?: string }) {
  const viewer = useRef<HTMLDivElement>(null);
  const pages = useRef<HTMLDivElement>(null);
  const retained = useRef(new Set<() => void>());
  const loadedTasks = useRef(new Map<() => void, number>());
  const nextGeneration = useRef(0);
  const visibleGeneration = useRef(0);
  const disposeVisible = useRef<(() => void) | null>(null);
  const [width, setWidth] = useState(0);
  const [document, setDocument] = useState<LoadedPdf | null>(null);
  const [retry, setRetry] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
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
      retained.current.delete(destroy);
      loadedTasks.current.delete(destroy);
      void task.destroy();
    };
    retained.current.add(destroy);
    void (async () => {
      const pdf = await task.promise;
      const ratios: number[] = [];
      // Page dimensions reserve the complete scroll range without rasterizing it.
      for (let number = 1; number <= pdf.numPages; number++) {
        if (disposed) return;
        const page = await pdf.getPage(number);
        const natural = page.getViewport({ scale: 1 });
        ratios.push(natural.height / natural.width);
      }
      if (!disposed) {
        loaded = true;
        loadedTasks.current.set(destroy, generation);
        setDocument({ pdf, ratios, generation, destroy });
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
      retained.current.forEach((destroy) => destroy());
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
    const pending = new Map<HTMLElement, Promise<void>>();
    const wanted = new Set<HTMLElement>();
    const layer = window.document.createElement("div");
    const slots = document.ratios.map((ratio, index) => {
      const slot = window.document.createElement("div");
      slot.className = "paper-pdf-page";
      slot.style.height = `${width * ratio}px`;
      slot.dataset.page = String(index + 1);
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
        const viewport = page.getViewport({
          scale: (width / natural.width) * Math.min(window.devicePixelRatio || 1, 2),
        });
        const canvas = window.document.createElement("canvas");
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        canvas.setAttribute("role", "img");
        canvas.setAttribute("aria-label", `PDF page ${slot.dataset.page}`);
        const job = page.render({ canvas, viewport });
        jobs.set(slot, job);
        try {
          await job.promise;
          if (!disposed && wanted.has(slot)) slot.replaceChildren(canvas);
        } catch (reason) {
          if (wanted.has(slot)) throw reason;
        } finally {
          jobs.delete(slot);
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
        position = root.scrollTop;
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
      } while (root.scrollTop !== position);
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
      slots.forEach((slot) => observer.observe(slot));
      setError(null);
      setLoading(false);
    })().catch(fail);
    function dispose() {
      disposed = true;
      observer.disconnect();
      wanted.clear();
      jobs.forEach((job) => job.cancel());
    }
    return () => {
      if (!committed) dispose();
    };
  }, [document, width]);

  return (
    <div ref={viewer} className={`paper-pdf${error ? " paper-failed" : ""}`}>
      <div className="paper-preview-actions">
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
      </div>
      <div className="paper-pdf-pages" ref={pages} />
    </div>
  );
}
