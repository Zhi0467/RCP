import "./paper-preview.css";
import {
  createContext,
  useContext,
  lazy,
  Suspense,
  memo,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Download,
  FileWarning,
  LoaderCircle,
  MessageSquarePlus,
  PackagePlus,
  RotateCcw,
  X,
} from "lucide-react";
import { api } from "../core/api";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import { ANSWER_SYNTAX_PLUGINS, KATEX_OPTIONS } from "../core/markdownSyntax";
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
import { panelTools } from "./panelTools";
import { synctexSpot, type SyncTexMap } from "./synctex";
import { usePreviewText } from "./usePreviewText";
import { htmlSourceSpot, rehypeSourceLines, type PreviewSourceSpot } from "./previewComments";
import { imageBoxAnchor } from "./previewAnchors";
import { blankFrontMatter } from "./frontMatter";
import { installBoxTool } from "../comments/box.ts";
import { selectionInput } from "../comments/selectionInput.ts";
import { boxArea, previewWhere } from "../comments/where.ts";
import type { CommentAnchor } from "../comments/types.ts";
import { useInlineShell } from "../artifacts/useInlineShell";
import { rehypeHeadingIds } from "./markdownHtml";
import type { PluggableList } from "unified";

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
  synctex?: SyncTexMap;
}

const PAPER_IMAGE = /\.(png|jpe?g|gif|webp|svg)$/i;
const HTML_ZOOM = { min: 50, max: 200, step: 5 };
const zoomStop = (percent: number) => Math.log2(percent / 100);
const zoomPercent = (stop: number) =>
  Math.round((100 * 2 ** stop) / HTML_ZOOM.step) * HTML_ZOOM.step;

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
  if (href.startsWith("#"))
    return (
      <a
        href={href}
        onClick={(event) => {
          event.preventDefault();
          const id = decodeURIComponent(href.slice(1));
          const article = event.currentTarget.closest("article");
          const target = [id, `user-content-${id}`]
            .map((name) => article?.querySelector(`[id="${CSS.escape(name)}"]`))
            .find(Boolean);
          target?.scrollIntoView({ behavior: "smooth", block: "start" });
        }}
      >
        {children}
      </a>
    );
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
  const image = tree.files.find((entry) => entry.path === path && PAPER_IMAGE.test(entry.path));
  return image ? (
    <img src={assetUrl(projectId, image)} alt={alt} />
  ) : (
    <span role="img" aria-label={alt}>
      {alt}
    </span>
  );
}
let rawHtmlPlugins: PluggableList | null = null;
/** Loads the raw-HTML renderer, kept out of the Paper chunk until a file has HTML. */
export async function loadRawHtml() {
  rawHtmlPlugins ??= (await import("./markdownRawHtml")).PAPER_RAW_HTML;
  return rawHtmlPlugins;
}
const HAS_HTML = /<[a-zA-Z!/]/;

// Tree saves and callbacks update links without reparsing the unchanged preview text.
const MarkdownPreview = memo(function MarkdownPreview({
  text,
  path,
}: {
  text: string;
  path: string;
}) {
  const [raw, setRaw] = useState(rawHtmlPlugins);
  const wantsRaw = HAS_HTML.test(text);
  useEffect(() => {
    if (wantsRaw && !raw) void loadRawHtml().then(setRaw);
  }, [wantsRaw, raw]);
  return (
    <article className="paper-markdown chat-markdown" data-paper-path={path}>
      <ReactMarkdown
        remarkPlugins={[...ANSWER_SYNTAX_PLUGINS]}
        rehypePlugins={[
          ...(wantsRaw && raw ? raw : []),
          rehypeHeadingIds,
          rehypeSourceLines,
          [rehypeKatex, KATEX_OPTIONS],
        ]}
        components={{ pre: MarkdownCodeBlock, a: PaperLink, img: PaperImage }}
      >
        {text}
      </ReactMarkdown>
    </article>
  );
});

/** A place drawn in the preview, as a thread anchor, and where it shows in the window. */
export interface PreviewAnchor {
  anchor: CommentAnchor;
  rect: { left: number; top: number; right: number; bottom: number };
  /** Removes the selection drawn for it, when its comment closes. */
  clear?: () => void;
}

/** A Paper image that takes boxes, in fractions of the image. */
function ImagePreview({
  url,
  path,
  onAnchor,
}: {
  url: string;
  path: string;
  onAnchor?: (drawn: PreviewAnchor) => void;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const image = useRef<HTMLImageElement>(null);
  const handler = useRef(onAnchor);
  handler.current = onAnchor;
  const boxes = Boolean(onAnchor);
  useEffect(() => {
    const surface = frame.current;
    if (!surface || !boxes) return;
    return installBoxTool(surface, (box) => {
      const bounds = surface.getBoundingClientRect();
      const left = bounds.left + box.x * bounds.width;
      const top = bounds.top + box.y * bounds.height;
      handler.current?.({
        anchor: imageBoxAnchor(image.current!, path, box),
        rect: {
          left,
          top,
          right: left + box.width * bounds.width,
          bottom: top + box.height * bounds.height,
        },
      });
    });
  }, [boxes, path]);
  return (
    <div className="paper-image-preview" data-paper-path={path}>
      <div className={`paper-image-frame${boxes ? " boxes" : ""}`} ref={frame}>
        <img ref={image} src={url} alt={path} draggable={false} />
      </div>
    </div>
  );
}

function useHtmlPreviewState(source: string, revision: number, hash: string, active: boolean) {
  const [preview, setPreview] = useState<{ key: string; source: string } | null>(null);
  const [entryReady, setEntryReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useLayoutEffect(() => setEntryReady(false), [source, revision, hash, active, attempt]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void api<{ revision: number; key: string }>(`${source}/state`, {
      signal: controller.signal,
    })
      .then((state) => {
        if (controller.signal.aborted) return;
        setPreview((previous) =>
          previous?.key === state.key
            ? previous
            : { key: state.key, source: `${source}?revision=${state.revision}` },
        );
        setError(null);
        setEntryReady(true);
      })
      .catch((reason) => {
        if (controller.signal.aborted) return;
        setError(String(reason));
        setEntryReady(true);
      });
    return () => controller.abort();
  }, [source, revision, hash, active, attempt]);
  return {
    preview,
    entryReady,
    error,
    retry: () => {
      setEntryReady(false);
      setAttempt((value) => value + 1);
    },
  };
}

/** A saved Paper HTML file in the inline viewer shell; comment mode selects inside it. */
function HtmlPreview({
  projectId,
  file,
  text,
  saved,
  savedRevision,
  active,
  toolbar,
  onAnchor,
  onAnchorCancel,
  onSourceSpot,
  onSelectionOffer,
}: {
  projectId: string;
  file: PaperFile;
  text: string;
  /** The editor holds what the frame shows, so a word's count matches the source. */
  saved: boolean;
  savedRevision: number;
  active: boolean;
  toolbar: HTMLElement | null;
  onAnchor?: (drawn: PreviewAnchor) => void;
  onAnchorCancel?: (clear: () => void) => void;
  onSourceSpot?: (spot: PreviewSourceSpot) => void;
  onSelectionOffer?: (drawn: PreviewAnchor | null) => void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [commentMode, setCommentMode] = useState(true);
  const [pageError, setPageError] = useState<{ message: string } | null>(null);
  const [zoom, setZoom] = useState(100);
  const source = `/api/projects/${encodeURIComponent(projectId)}/paper/files/${encodeURIComponent(
    file.file_id,
  )}/preview`;
  const {
    preview,
    entryReady,
    error: stateError,
    retry,
  } = useHtmlPreviewState(source, savedRevision, file.sha256, active);
  const shell = useInlineShell(frame, commentMode, {
    onPageError: setPageError,
    onSourceWord: (word, occurrence) => {
      if (!saved) return;
      const spot = htmlSourceSpot(file.path, text, word, occurrence);
      if (spot) onSourceSpot?.(spot);
    },
    onTextOffer: (offer) => {
      if (commentMode) return;
      onSelectionOffer?.(
        offer && {
          anchor: {
            kind: "text",
            text: offer.text,
            surrounding_text: offer.text,
            comment: "",
            where: previewWhere(file.path),
          },
          rect: offer.rect,
          clear: offer.clear,
        },
      );
    },
    onCancel: (clear) => onAnchorCancel?.(clear),
    onSelection: ({ selection, anchor, clear }) => {
      const read = selectionInput(selection);
      if (!read) return;
      const where =
        selection.kind === "box"
          ? `${file.path}, ${boxArea(selection.rect)}`
          : previewWhere(file.path);
      onAnchor?.({
        anchor: { ...read, where },
        rect: { left: anchor.left, right: anchor.right, top: anchor.top, bottom: anchor.top },
        clear,
      });
    },
  });
  const zoomTo = (next: number) => {
    shell.clear();
    setZoom(Math.max(HTML_ZOOM.min, Math.min(HTML_ZOOM.max, next)));
  };
  const scale = zoom / 100;
  const error = stateError || pageError?.message;
  return (
    <div
      className={`paper-html-preview${commentMode ? " commenting" : ""}${error ? " paper-failed" : ""}`}
      aria-busy={!entryReady}
    >
      {panelTools(
        toolbar,
        <>
          {stateError ? (
            <button
              className="icon-button"
              aria-label="Retry HTML preview"
              title={stateError}
              onClick={retry}
            >
              <RotateCcw size={16} />
            </button>
          ) : pageError ? (
            <span
              className="icon-button"
              role="status"
              title={pageError.message}
              aria-label={pageError.message}
            >
              <FileWarning size={16} />
            </span>
          ) : null}
          <span className="paper-html-zoom">
            <input
              type="range"
              aria-label="Preview zoom"
              min={zoomStop(HTML_ZOOM.min)}
              max={zoomStop(HTML_ZOOM.max)}
              step="0.01"
              value={zoomStop(zoom)}
              aria-valuetext={`${zoom}%`}
              disabled={!preview}
              onChange={(event) => zoomTo(zoomPercent(Number(event.target.value)))}
            />
            <button
              type="button"
              aria-label="Reset preview zoom"
              title="Reset preview zoom"
              disabled={!preview || zoom === 100}
              onClick={() => zoomTo(100)}
            >
              {zoom}%
            </button>
          </span>
          {onAnchor && (
            <button
              className="icon-button"
              aria-pressed={commentMode}
              aria-label="Comment on the page"
              title="Comment on the page"
              onClick={() => setCommentMode(!commentMode)}
            >
              <MessageSquarePlus size={16} />
            </button>
          )}
        </>,
        Boolean(error),
      )}
      {!entryReady && (
        <LoaderCircle
          className="spin paper-preview-loading"
          aria-label="Loading preview"
          size={20}
        />
      )}
      {preview && (
        <iframe
          key={preview.key}
          ref={frame}
          src={preview.source}
          title={file.path}
          onLoad={shell.onLoad}
          style={{
            width: `${100 / scale}%`,
            height: `${100 / scale}%`,
            transform: `scale(${scale})`,
            visibility: entryReady && !stateError ? undefined : "hidden",
          }}
        />
      )}
    </div>
  );
}

export function PaperPreview({
  projectId,
  file,
  tree,
  text,
  active,
  onOpenFile,
  toolbar,
  cursorLine,
  onAnchor,
  onAnchorCancel,
  onSourceSpot,
  onSelectionOffer,
}: {
  projectId: string;
  file: PaperFile;
  tree: PaperTree;
  text: string;
  /** A hidden preview stays mounted but does no compile work. */
  active: boolean;
  onOpenFile?: (fileId: string) => void;
  /** The header slot the preview's actions render into, while the preview is the shown panel. */
  toolbar: HTMLElement | null;
  /** The editor's settled cursor line in this file; a LaTeX PDF scrolls to it. */
  cursorLine: number;
  /** A box drawn over a PDF page or image, or a selection inside an HTML page, to comment on. */
  onAnchor?: (drawn: PreviewAnchor) => void;
  /** The HTML frame cancelled the selection drawn with this clear. */
  onAnchorCancel?: (clear: () => void) => void;
  /** A word double-clicked inside an HTML page, placed in its source. */
  onSourceSpot?: (spot: PreviewSourceSpot) => void;
  /** Text selected inside an HTML page, offered Comment as a native selection is. */
  onSelectionOffer?: (drawn: PreviewAnchor | null) => void;
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
  const [synctex, setSynctex] = useState<SyncTexMap>({});
  const spot = useMemo(
    () => synctexSpot(synctex, file.path, cursorLine),
    [synctex, file.path, cursorLine],
  );
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
            setSynctex(result.synctex ?? {});
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
          setSynctex({});
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

  const onBox =
    onAnchor &&
    ((anchor: CommentAnchor, rect: DOMRect) =>
      onAnchor({
        anchor,
        rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
      }));

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
        <MarkdownPreview text={blankFrontMatter(previewText)} path={file.path} />
      </PaperMarkdownContext.Provider>
    );
  if (file.format === "image" && PAPER_IMAGE.test(file.path))
    return <ImagePreview url={assetUrl(projectId, file)} path={file.path} onAnchor={onAnchor} />;
  if (file.format === "html")
    return (
      <HtmlPreview
        key={file.file_id}
        projectId={projectId}
        file={file}
        text={text}
        saved={tree.revision === tree.saved_revision}
        savedRevision={tree.saved_revision}
        active={active}
        toolbar={toolbar}
        onAnchor={onAnchor}
        onAnchorCancel={onAnchorCancel}
        onSourceSpot={onSourceSpot}
        onSelectionOffer={onSelectionOffer}
      />
    );
  if (file.format === "pdf")
    return (
      <Suspense fallback={<LoaderCircle className="spin" size={16} />}>
        <PaperPdf
          url={assetUrl(projectId, file)}
          name={file.path.split("/").at(-1)}
          toolbar={toolbar}
          path={file.path}
          onBox={onBox}
        />
      </Suspense>
    );
  if (!engine)
    return file.kind === "text" ? (
      <pre className="paper-plain-preview" data-paper-path={file.path}>
        {text}
      </pre>
    ) : (
      panelTools(
        toolbar,
        <a
          className="icon-button"
          href={paperRawUrl(projectId, file.file_id)}
          download
          aria-label="Download file"
        >
          <Download size={16} />
        </a>,
      )
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
      {!pdf && panelTools(toolbar, actions)}
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
            toolbar={toolbar}
            spot={spot}
            path={pdfRoot}
            synctex={synctex}
            onBox={onBox}
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
