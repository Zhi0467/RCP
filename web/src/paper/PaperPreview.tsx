import "./paper-preview.css";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Download, LoaderCircle, RotateCcw } from "lucide-react";
import { api } from "../core/api";
import { MarkdownAnswer } from "../core/chatMarkdown";
import type { PaperFile, PaperTree } from "../core/types";
import {
  compileTypst,
  paperRawUrl,
  paperRelativePath,
  readTypstTree,
  typstRoots,
} from "./typstPreview";

const PaperPdf = lazy(() => import("./PaperPdf"));
type EngineState = "ready" | "installing" | "unavailable";
type Engines = { latex: EngineState; typst: EngineState };
interface CompileResult {
  status: "ok" | "error" | "superseded" | "unavailable";
  root_path: string;
  candidates?: string[];
  log_tail: string;
  pdf_url: string | null;
}

export function PaperPreview({
  projectId,
  file,
  tree,
  text,
}: {
  projectId: string;
  file: PaperFile;
  tree: PaperTree;
  text: string;
}) {
  const [engines, setEngines] = useState<Engines | null>(null);
  const [pdf, setPdf] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<string[]>([]);
  const [selectedRoot, setSelectedRoot] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);
  const engine = file.format === "latex" || file.format === "typst" ? file.format : null;
  const base = `/api/projects/${encodeURIComponent(projectId)}/paper`;
  const typstText = engine === "typst" ? text : "";
  // A tree revision owns its file metadata. Polling the same revision must not
  // restart a compile; LaTeX additionally waits for that durable revision.
  const filesRef = useRef(tree.files);
  useEffect(() => {
    filesRef.current = tree.files;
  }, [tree.files]);
  useEffect(
    () => () => {
      if (pdf?.startsWith("blob:")) URL.revokeObjectURL(pdf);
    },
    [pdf],
  );

  useEffect(() => {
    setSelectedRoot("");
    setCandidates([]);
    setPdf(null);
    setError(null);
  }, [projectId, file.file_id]);
  useEffect(() => {
    if (!engine) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const check = async () => {
      try {
        const value = await api<Engines>(`${base}/engines`, { signal: controller.signal });
        setEngines(value);
        if (value[engine] === "installing") timer = setTimeout(() => void check(), 1500);
      } catch (reason) {
        if (!controller.signal.aborted) setError(String(reason));
      }
    };
    void check();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [base, engine, retry]);

  useEffect(() => {
    setLoading(false);
    if (!engine || engines?.[engine] !== "ready") return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setError(null);
      setLoading(true);
      void (async () => {
        const files = filesRef.current;
        if (engine === "latex") {
          const candidate = files.find((entry) => entry.path === selectedRoot);
          const result = await api<CompileResult>(`${base}/compile`, {
            method: "POST",
            body: JSON.stringify({
              engine,
              file_id: candidate?.file_id ?? file.file_id,
              revision: tree.saved_revision,
            }),
            signal: controller.signal,
          });
          if (controller.signal.aborted) return;
          setCandidates(result.candidates ?? []);
          if (result.status === "ok" && result.pdf_url) {
            const target = new URL(result.pdf_url, window.location.origin);
            if (
              target.origin !== window.location.origin ||
              !target.pathname.startsWith(`${base}/compile/`)
            ) {
              throw new Error("Invalid PDF preview URL");
            }
            setPdf(target.href);
          } else if (result.status === "unavailable") {
            setEngines((value) => value && { ...value, latex: "unavailable" });
          } else if (result.status === "error" && !result.candidates?.length) {
            throw new Error(result.log_tail || "Compilation failed");
          } else if (result.status !== "superseded") {
            setPdf(null);
          }
        } else {
          const openFile = files.find((entry) => entry.file_id === file.file_id);
          if (!openFile) throw new Error("The open file is no longer in the paper tree");
          const sources = await readTypstTree(
            projectId,
            files,
            tree.saved_revision,
            openFile,
            typstText,
            controller.signal,
          );
          const typst = new Map(
            sources
              .filter((source) => source.path.endsWith(".typ"))
              .map((source) => [source.path, new TextDecoder().decode(source.bytes)]),
          );
          const roots = typstRoots(file.path, typst);
          if (controller.signal.aborted) return;
          setCandidates(roots.length > 1 ? roots : []);
          const root = roots.includes(selectedRoot)
            ? selectedRoot
            : roots.length === 1
              ? roots[0]
              : null;
          if (!root) {
            setPdf(null);
            return;
          }
          const bytes = await compileTypst(sources, root, controller.signal);
          if (controller.signal.aborted) return;
          const objectUrl = URL.createObjectURL(
            new Blob([new Uint8Array(bytes)], { type: "application/pdf" }),
          );
          setPdf(objectUrl);
        }
      })()
        .catch((reason: unknown) => {
          if (!controller.signal.aborted)
            setError(reason instanceof Error ? reason.message : String(reason));
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 600);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [
    base,
    engine,
    engines,
    file.file_id,
    file.path,
    projectId,
    tree.saved_revision,
    typstText,
    selectedRoot,
    retry,
  ]);

  async function install() {
    if (!engine) return;
    setError(null);
    try {
      await api(`${base}/engines/${engine}/install`, { method: "POST" });
      setRetry((value) => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  if (file.format === "markdown")
    return (
      <article className="paper-markdown chat-markdown">
        <MarkdownAnswer
          text={text}
          renderEmbed={(src, alt) => {
            const path = paperRelativePath(file.path, src);
            const image = tree.files.find(
              (entry) => entry.path === path && /\.(png|jpe?g|gif|webp)$/i.test(entry.path),
            );
            return image ? (
              <img src={paperRawUrl(projectId, image.file_id, tree.saved_revision)} alt={alt} />
            ) : (
              <span role="img" aria-label={alt}>
                {alt}
              </span>
            );
          }}
        />
      </article>
    );
  if (file.format === "image" && /\.(png|jpe?g|gif|webp)$/i.test(file.path))
    return (
      <div className="paper-image-preview">
        <img src={paperRawUrl(projectId, file.file_id, tree.saved_revision)} alt={file.path} />
      </div>
    );
  if (file.format === "pdf")
    return (
      <Suspense fallback={<LoaderCircle className="spin" size={16} />}>
        <PaperPdf
          url={paperRawUrl(projectId, file.file_id, tree.saved_revision)}
          name={file.path.split("/").at(-1)}
        />
      </Suspense>
    );
  if (!engine)
    return file.kind === "text" ? (
      <pre className="paper-plain-preview">{text}</pre>
    ) : (
      <a
        className="icon-button"
        href={paperRawUrl(projectId, file.file_id)}
        download
        aria-label="Download file"
      >
        <Download size={16} />
      </a>
    );

  return (
    <div className={`paper-compiled-preview${error ? " paper-failed" : ""}`} aria-busy={loading}>
      <div className="paper-preview-actions">
        {candidates.length > 1 && (
          <select
            aria-label="Preview root"
            value={selectedRoot}
            onChange={(event) => {
              setPdf(null);
              setSelectedRoot(event.target.value);
            }}
          >
            <option value="" disabled>
              Choose document
            </option>
            {candidates.map((path) => (
              <option key={path} value={path}>
                {path}
              </option>
            ))}
          </select>
        )}
        {(loading || engines?.[engine] === "installing") && (
          <LoaderCircle
            className="spin"
            size={16}
            aria-label={loading ? "Compiling" : "Installing engine"}
          />
        )}
        {engines?.[engine] === "unavailable" && !error && (
          <button
            className="icon-button"
            aria-label={`Install ${engine}`}
            title={`Install ${engine}`}
            onClick={() => void install()}
          >
            <Download size={16} />
          </button>
        )}
        {error && (
          <button
            className="icon-button"
            title={error}
            aria-label="Retry preview"
            onClick={() =>
              engines?.[engine] === "unavailable" ? void install() : setRetry((value) => value + 1)
            }
          >
            <RotateCcw size={16} />
          </button>
        )}
      </div>
      {pdf && (
        <Suspense fallback={<LoaderCircle className="spin" size={16} />}>
          <PaperPdf
            url={pdf}
            name={`${(selectedRoot || file.path)
              .split("/")
              .at(-1)
              ?.replace(/\.[^.]+$/, "")}.pdf`}
          />
        </Suspense>
      )}
    </div>
  );
}
