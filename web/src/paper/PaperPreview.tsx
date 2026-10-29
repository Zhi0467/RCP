import "./paper-preview.css";
import {
  createContext,
  useContext,
  lazy,
  Suspense,
  memo,
  useEffect,
  useRef,
  useState,
} from "react";
import { Download, FileWarning, LoaderCircle, PackagePlus, RotateCcw, X } from "lucide-react";
import { api } from "../core/api";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import { ANSWER_SYNTAX_PLUGINS } from "../core/chatMarkdown";
import { MarkdownCodeBlock } from "../core/MarkdownCodeBlock";
import type { PaperFile, PaperTree } from "../core/types";
import {
  TypstCompiler,
  paperRawUrl,
  markdownPaperPath,
  readTypstTree,
  typstRoots,
} from "./typstPreview";

import { PaperDialog } from "./PaperDialog";
import { usePreviewText } from "./usePreviewText";

const PaperPdf = lazy(() => import("./PaperPdf"));
type EngineState = "ready" | "installing" | "unavailable";
type Engines = {
  latex: EngineState;
  typst: EngineState;
  errors: { latex: string | null; typst: string | null };
};
interface CompileResult {
  status: "ok" | "error" | "superseded" | "unavailable";
  root_path: string;
  candidates?: string[];
  log_tail: string;
  pdf_url: string | null;
}

// An asset URL changes only with its bytes, so other files' saves never refetch it.
function assetUrl(projectId: string, file: PaperFile) {
  return `${paperRawUrl(projectId, file.file_id)}?sha256=${file.sha256}`;
}

type MarkdownContext = {
  file: PaperFile;
  tree: PaperTree;
  projectId: string;
  onOpenFile?: (fileId: string) => void;
};
const PaperMarkdownContext = createContext<MarkdownContext>(null!);
function PaperLink({ href = "", children }: { href?: string; children?: React.ReactNode }) {
  const { file, tree, onOpenFile } = useContext(PaperMarkdownContext);
  if (/^https?:\/\//i.test(href))
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  const path = markdownPaperPath(file.path, href);
  const target = tree.files.find((entry) => entry.path === path);
  return target && onOpenFile ? (
    <a
      href="#"
      onClick={(event) => {
        event.preventDefault();
        onOpenFile(target.file_id);
      }}
    >
      {children}
    </a>
  ) : (
    <span>{children}</span>
  );
}
function PaperImage({ src = "", alt = "" }: { src?: string | Blob; alt?: string }) {
  const { file, tree, projectId } = useContext(PaperMarkdownContext);
  const path = markdownPaperPath(file.path, String(src));
  const image = tree.files.find(
    (entry) => entry.path === path && /\.(png|jpe?g|gif|webp)$/i.test(entry.path),
  );
  return image ? (
    <img src={assetUrl(projectId, image)} alt={alt} />
  ) : (
    <span role="img" aria-label={alt}>
      {alt}
    </span>
  );
}
// Tree saves and callbacks update links without reparsing the unchanged preview text.
const MarkdownPreview = memo(function MarkdownPreview({ text }: { text: string }) {
  return (
    <article className="paper-markdown chat-markdown">
      <ReactMarkdown
        remarkPlugins={[...ANSWER_SYNTAX_PLUGINS]}
        rehypePlugins={[[rehypeKatex, { strict: false, trust: false }]]}
        components={{ pre: MarkdownCodeBlock, a: PaperLink, img: PaperImage }}
      >
        {text}
      </ReactMarkdown>
    </article>
  );
});

export function PaperPreview({
  projectId,
  file,
  tree,
  text,
  active,
  onOpenFile,
}: {
  projectId: string;
  file: PaperFile;
  tree: PaperTree;
  text: string;
  /** A hidden preview stays mounted but does no compile work. */
  active: boolean;
  onOpenFile?: (fileId: string) => void;
}) {
  const previewText = usePreviewText(file.format === "markdown" ? text : "", file.file_id, active);
  const compiled = useRef<string | null>(null);
  const compiler = useRef(new TypstCompiler());
  const bytesCache = useRef(new Map<string, Uint8Array>());
  const [showLog, setShowLog] = useState(false);
  useEffect(() => () => compiler.current.dispose(), []);
  const [engines, setEngines] = useState<Engines | null>(null);
  const [pdfRoot, setPdfRoot] = useState("");
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
    compiled.current = null;
    setSelectedRoot("");
    setCandidates([]);
    setPdf(null);
    setError(null);
    setShowLog(false);
  }, [projectId, file.file_id]);
  useEffect(() => {
    if (!engine) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const check = async () => {
      try {
        const value = await api<Engines>(`${base}/engines`, { signal: controller.signal });
        setEngines(value);
        if (value.errors[engine]) setError(value.errors[engine]);
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
    if (!engine || engines?.[engine] !== "ready" || !active) return;
    // Showing the preview again compiles only when its input changed meanwhile.
    const key = JSON.stringify([file.file_id, tree.saved_revision, typstText, selectedRoot, retry]);
    if (key === compiled.current) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setError(null);
      setLoading(true);
      void (async () => {
        const files = filesRef.current;
        if (engine === "latex") {
          const result = await api<CompileResult>(`${base}/compile`, {
            method: "POST",
            body: JSON.stringify({
              file_id: file.file_id,
              revision: tree.saved_revision,
              root_path: selectedRoot || null,
            }),
            signal: controller.signal,
          });
          if (controller.signal.aborted) return;
          setCandidates(result.candidates ?? []);
          if (result.status === "ok" && result.pdf_url) {
            setPdfRoot(result.root_path);
            setPdf(result.pdf_url);
          } else if (result.status === "unavailable") {
            setEngines((value) => value && { ...value, latex: "unavailable" });
          } else if (result.status === "error" && !result.candidates?.length) {
            throw new Error(result.log_tail || "Compilation failed");
          } else if (result.status === "superseded") {
            // Another viewer's different compile replaced this one: offer Retry.
            throw new Error("Another preview of this paper replaced this compile.");
          } else {
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
            bytesCache.current,
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
          const bytes = await compiler.current.compile(sources, root, controller.signal);
          if (controller.signal.aborted) return;
          const objectUrl = URL.createObjectURL(
            new Blob([new Uint8Array(bytes)], { type: "application/pdf" }),
          );
          setPdfRoot(root);
          setPdf(objectUrl);
        }
      })()
        .catch((reason: unknown) => {
          if (!controller.signal.aborted)
            setError(reason instanceof Error ? reason.message : String(reason));
        })
        .finally(() => {
          if (controller.signal.aborted) return;
          compiled.current = key;
          setLoading(false);
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
    active,
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
      <PaperMarkdownContext.Provider value={{ file, tree, projectId, onOpenFile }}>
        <MarkdownPreview text={previewText} />
      </PaperMarkdownContext.Provider>
    );
  if (file.format === "image" && /\.(png|jpe?g|gif|webp)$/i.test(file.path))
    return (
      <div className="paper-image-preview">
        <img src={assetUrl(projectId, file)} alt={file.path} />
      </div>
    );
  if (file.format === "pdf")
    return (
      <Suspense fallback={<LoaderCircle className="spin" size={16} />}>
        <PaperPdf url={assetUrl(projectId, file)} name={file.path.split("/").at(-1)} />
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

  const actions = (
    <>
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
          <PackagePlus size={16} />
        </button>
      )}
      {error && (
        <button
          className="icon-button"
          aria-label="View compile log"
          onClick={() => setShowLog(true)}
        >
          <FileWarning size={16} />
        </button>
      )}
    </>
  );

  return (
    <div className={`paper-compiled-preview${error ? " paper-failed" : ""}`} aria-busy={loading}>
      {!pdf && <div className="paper-preview-actions paper-preview-empty">{actions}</div>}
      {showLog && error && (
        <PaperDialog label="Compile log" onClose={() => setShowLog(false)}>
          <header className="paper-bar">
            <strong>Compile log</strong>
            <button
              className="icon-button"
              aria-label="Retry preview"
              onClick={() => {
                setShowLog(false);
                if (engines?.[engine] === "unavailable") void install();
                else setRetry((value) => value + 1);
              }}
            >
              <RotateCcw size={16} />
            </button>
            <button
              className="icon-button"
              aria-label="Close compile log"
              onClick={() => setShowLog(false)}
            >
              <X size={16} />
            </button>
          </header>
          <pre className="paper-plain-preview" tabIndex={0}>
            {error}
          </pre>
        </PaperDialog>
      )}
      {pdf && (
        <Suspense fallback={<LoaderCircle className="spin" size={16} />}>
          <PaperPdf
            key={`${projectId}:${pdfRoot}`}
            scrollKey={`${projectId}:${pdfRoot}`}
            actions={actions}
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
