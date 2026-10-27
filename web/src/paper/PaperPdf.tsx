import { useEffect, useRef, useState } from "react";
import { Download, LoaderCircle, RotateCcw } from "lucide-react";
import { getDocument, GlobalWorkerOptions } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

GlobalWorkerOptions.workerSrc = workerUrl;

export default function PaperPdf({ url, name = "paper.pdf" }: { url: string; name?: string }) {
  const pages = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [retry, setRetry] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const container = pages.current;
    if (!container) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)));
    observer.observe(container);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const container = pages.current;
    if (!container || !width) return;
    let disposed = false;
    setError(null);
    setLoading(true);
    container.replaceChildren();
    const task = getDocument({ url, enableXfa: false });
    void (async () => {
      const pdf = await task.promise;
      for (let number = 1; number <= pdf.numPages; number++) {
        if (disposed) return;
        const page = await pdf.getPage(number);
        if (disposed) return;
        const natural = page.getViewport({ scale: 1 });
        const scale = width / natural.width;
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const viewport = page.getViewport({ scale: scale * ratio });
        const canvas = document.createElement("canvas");
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        canvas.style.width = "100%";
        canvas.style.height = "auto";
        canvas.setAttribute("role", "img");
        canvas.setAttribute("aria-label", `PDF page ${number}`);
        container.append(canvas);
        await page.render({ canvas, viewport }).promise;
      }
      if (!disposed) setLoading(false);
    })().catch((reason: unknown) => {
      if (!disposed) {
        setLoading(false);
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    });
    return () => {
      disposed = true;
      void task.destroy();
    };
  }, [url, width, retry]);
  return (
    <div className={`paper-pdf${error ? " paper-failed" : ""}`}>
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
