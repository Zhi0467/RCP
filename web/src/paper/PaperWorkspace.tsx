import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { EditorView } from "@codemirror/view";
import {
  Download,
  FileWarning,
  History,
  LoaderCircle,
  MessageSquare,
  PanelLeft,
  RotateCcw,
  X,
} from "lucide-react";
import { api } from "../core/api";
import { readPref, writePref } from "../core/prefStorage";
import type {
  AgentTask,
  IdentityResponse,
  PaperFile,
  ProjectSnapshot,
  PublicHealth,
} from "../core/types";
import type { StartConversationTask } from "../chat/chatWorkspace";
import { useRefreshRuntime, useResource } from "../core/useResource";
import { resourceKeyId } from "../core/resourceStore";
import { paperProfile } from "./paperResources";
import {
  acquirePaperDocument,
  observePaperBackend,
  paperBackendKey,
  recoverUnsentPaperDocuments,
  resumePaperDocuments,
  unsentPaperDocuments,
  type PaperDocument,
} from "./paperDocuments";
import { carryPaperSelection, PaperEditor, type PaperFocus } from "./PaperEditor";
import {
  PaperReferences,
  PaperReferencePage,
  usePaperReferences,
  type PaperReference,
} from "./PaperReferences";
import { PaperExport } from "./PaperExport";
import { PaperGitChanges, PaperGitDiff, PaperGitPill, usePaperGit } from "./PaperGit";
import { findReference } from "./referenceFiles";
import { citation } from "./citeKeys";
import { PaperFiles } from "./PaperFiles";
import { usePaperFilesSplit, usePaperSplit } from "./usePaperSplit";
import "./paper-split.css";
import { ResizeHandle } from "../ui/ResizeHandle";
import { PaperPreview } from "./PaperPreview";
import { PaperCoach, type CoachChatActions, type CoachDiff, type CoachOpening } from "./PaperCoach";
import { PaperTurnDiff } from "./PaperTurn";
import { PaperHistory } from "./PaperHistory";
import { previewSourceSpot, type PreviewSourceSpot } from "./previewComments";
import { usePaperComments } from "./PaperComments";
import type { SendThreads } from "../comments/react/useComments";

/** Route snapshots retain where the member was; IndexedDB owns all document content. */
export interface PaperEditorState {
  fileId?: string;
  view?: "write" | "preview" | "coach" | "refs";
  right?: "preview" | "coach" | "reference";
  coach?: CoachOpening;
}
interface Props {
  apiBase: string;
  project: ProjectSnapshot;
  editorState?: PaperEditorState;
  requestedFileId?: { fileId: string | null };
  onEditorStateChange?: (state: PaperEditorState) => void;
  tasks: AgentTask[];
  /** Starts a task; a Coach turn carrying threads passes its Idempotency-Key. */
  onStartTask: StartConversationTask;
  coachActions: CoachChatActions;
}
const FOLD_KEY = "rcp:paper-files-folded";
const NO_CHANGES: [] = [];
const openedLinks = new WeakSet<object>();
export function PaperWorkspace({
  apiBase,
  project,
  editorState,
  requestedFileId,
  onEditorStateChange,
  tasks,
  onStartTask,
  coachActions,
}: Props) {
  const { store } = useRefreshRuntime();
  const profile = paperProfile(project.id);
  const resource = useResource(`${project.id}:paper-view`, profile);
  const tree = resource.value?.tree;
  const references = usePaperReferences(project.id, tree);
  const [reference, setReference] = useState<{
    key: string;
    fileId?: string;
  } | null>(null);
  const referenceEntry = findReference(references.entries, reference);
  const [identity, setIdentity] = useState<IdentityResponse | null>(null);
  const [identityError, setIdentityError] = useState<string | null>(null);
  const [identityRetry, setIdentityRetry] = useState(0);
  // The tree revision at which the selected file is known to exist: a tree at or past it
  // that lacks the file means it is gone, while an older one may predate a file just
  // created. A listed file or a link records the current revision, a create its receipt's.
  const [selection, setSelection] = useState<{ id: string | null; existsAt: number }>(() => ({
    id:
      (requestedFileId && !openedLinks.has(requestedFileId) ? requestedFileId.fileId : null) ??
      editorState?.fileId ??
      null,
    existsAt: -1,
  }));
  const selected = selection.id;
  const treeRevision = useRef(-1);
  treeRevision.current = tree?.revision ?? -1;
  const setSelected = useCallback(
    (id: string | null, existsAt = treeRevision.current) => setSelection({ id, existsAt }),
    [],
  );
  useEffect(() => {
    if (!requestedFileId || openedLinks.has(requestedFileId)) return;
    openedLinks.add(requestedFileId);
    if (requestedFileId.fileId) setSelected(requestedFileId.fileId);
  }, [requestedFileId, setSelected]);
  const remembered = useRef<PaperEditorState>({ ...editorState });
  const remember = useCallback(
    (part: PaperEditorState) => {
      remembered.current = { ...remembered.current, ...part };
      onEditorStateChange?.(remembered.current);
    },
    [onEditorStateChange],
  );
  const [folded, setFolded] = useState(() => readPref(FOLD_KEY) === "true");
  const [filesOpen, setFilesOpen] = useState(false);
  const [view, setView] = useState(editorState?.view ?? "write");
  const [right, setRight] = useState(editorState?.right ?? "preview");
  useEffect(() => remember({ view, right }), [view, right, remember]);
  const rememberCoach = useCallback((coach: CoachOpening) => remember({ coach }), [remember]);
  const [phone, setPhone] = useState(() => window.matchMedia("(max-width: 700px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 700px)");
    const changed = () => setPhone(media.matches);
    media.addEventListener("change", changed);
    return () => media.removeEventListener("change", changed);
  }, []);
  const writingPane = useRef<HTMLDivElement>(null);
  // The coach's History and New chat join the row that switches the Paper view.
  const [panelBar, setPanelBar] = useState<HTMLElement | null>(null);
  // The open file's settled cursor line; the preview follows it through SyncTeX.
  const [cursor, setCursor] = useState<{ file: string | null; line: number }>({
    file: null,
    line: 1,
  });
  const [history, setHistory] = useState(false);
  const [focus, setFocus] = useState<PaperFocus | null>(null);
  const [actionError, setActionError] = useState<{
    error: string;
    retry: () => Promise<void>;
  } | null>(null);
  const previewPane = useRef<HTMLDivElement>(null);
  /** The Coach composer's sender, which Paper threads ride; the coach sets it while mounted. */
  const coachThreads = useRef<SendThreads | null>(null);
  const sendThreads = useCallback<SendThreads>((ids, key) => {
    const send = coachThreads.current;
    return send ? send(ids, key) : Promise.reject(new Error("The coach is not ready."));
  }, []);
  const refresh = useCallback(
    () => store.invalidate(resourceKeyId(paperProfile(project.id).key)),
    [store, project.id],
  );
  useEffect(() => {
    const controller = new AbortController();
    // The previous identity stays until this one is verified, so an open editor keeps
    // taking input across a backend restart; its document waits, paused, meanwhile.
    void Promise.all([
      api<IdentityResponse>("/api/identity", { signal: controller.signal }),
      api<PublicHealth>("/api/health", { signal: controller.signal }),
    ])
      .then(([value, health]) => {
        if (controller.signal.aborted) return;
        observePaperBackend({ ok: true, health, message: null, reason: "paper-open" });
        resumePaperDocuments(paperBackendKey(apiBase, value.space_id), value.user.user_id);
        setIdentity((current) =>
          current?.space_id === value.space_id &&
          current.user.user_id === value.user.user_id &&
          current.user.display_name === value.user.display_name
            ? current
            : value,
        );
        setIdentityError(null);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setIdentityError(String(error));
      });
    return () => controller.abort();
  }, [apiBase, identityRetry]);
  useEffect(() => {
    const verify = () => setIdentityRetry((value) => value + 1);
    window.addEventListener("rcp:paper-backend-changed", verify);
    window.addEventListener("rcp:session-required", verify);
    return () => {
      window.removeEventListener("rcp:paper-backend-changed", verify);
      window.removeEventListener("rcp:session-required", verify);
    };
  }, []);
  const opened = useRef<{ file: PaperFile; document: PaperDocument | null } | null>(null);
  const last = opened.current;
  const current = selected ?? last?.file.file_id;
  const listed = tree?.files.find((item) => item.file_id === current);
  // A file that left the tree stays reachable, read-only, while it holds unsent local text.
  const unsent = useSyncExternalStore(
    unsentPaperDocuments.subscribe,
    unsentPaperDocuments.getSnapshot,
  );
  const backend = identity && paperBackendKey(apiBase, identity.space_id);
  const strandedFiles = unsent.flatMap((owner) =>
    tree &&
    owner.listing &&
    owner.key.backend === backend &&
    owner.key.member === identity?.user.user_id &&
    owner.key.project === project.id &&
    !tree.files.some((item) => item.file_id === owner.key.file)
      ? [owner.listing]
      : [],
  );
  const stranded = listed ? undefined : strandedFiles.find((item) => item.file_id === current);
  // A reload finds unsent files the tree no longer lists in this member's local list.
  useEffect(() => {
    if (!identity || !tree) return;
    recoverUnsentPaperDocuments(
      {
        backend: paperBackendKey(apiBase, identity.space_id),
        member: identity.user.user_id,
        project: project.id,
      },
      new Set(tree.files.map((item) => item.file_id)),
      apiBase,
      identity.user.display_name || identity.user.user_id,
    );
  }, [identity, tree, project.id, apiBase]);
  const gone = tree !== undefined && tree.revision >= selection.existsAt;
  const file = listed ?? stranded ?? (!selected || gone ? tree?.files[0] : undefined);
  const strandedElsewhere = strandedFiles.find((item) => item.file_id !== file?.file_id);
  // A selection waiting for a newer tree asks for it once rather than show nothing.
  useEffect(() => {
    if (tree && selection.existsAt > tree.revision) refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one read per tree revision that is still behind
  }, [tree?.revision, selection.existsAt, refresh]);
  const document = useMemo(
    () =>
      identity && file?.kind === "text"
        ? acquirePaperDocument(
            {
              backend: paperBackendKey(apiBase, identity.space_id),
              member: identity.user.user_id,
              project: project.id,
              file: file.file_id,
            },
            apiBase,
            identity.user.display_name || identity.user.user_id,
          )
        : null,
    [identity, file?.file_id, file?.kind, project.id, apiBase],
  );
  useEffect(() => document?.retain(), [document]);
  useEffect(() => {
    opened.current = file ? { file, document } : null;
    if (document && listed && file === listed) document.listing = listed;
  }, [file, listed, document]);
  // After a 4403 close the tree read decides between Recreate and Reconnect; until it
  // lands neither is offered.
  const resourceNow = useRef(resource);
  resourceNow.current = resource;
  const [closeCheck, setCloseCheck] = useState<{
    document: PaperDocument;
    seen: typeof resource;
  } | null>(null);
  const checking =
    closeCheck !== null &&
    closeCheck.document === document &&
    closeCheck.seen.value === resource.value &&
    closeCheck.seen.error === resource.error;
  useEffect(() => {
    if (!document) return;
    let lastSavedText: string | null = null;
    let lastClosed: number | null = null;
    let timer: ReturnType<typeof setTimeout>;
    const unsubscribe = document.subscribe(() => {
      const state = document.getSnapshot();
      // 4403: the file was deleted or access was lost; the tree says which.
      if (state.closed !== lastClosed) {
        lastClosed = state.closed;
        if (lastClosed === 4403) {
          setCloseCheck({ document, seen: resourceNow.current });
          refresh();
          // A deleted file with nothing unsent leaves for the first file.
          if (state.saved) setSelected(null);
        }
      }
      if (state.saved && state.text !== lastSavedText) {
        lastSavedText = state.text;
        clearTimeout(timer);
        timer = setTimeout(refresh, 600);
      }
    });
    return () => {
      unsubscribe();
      clearTimeout(timer);
    };
  }, [document, refresh, setSelected]);
  useEffect(() => {
    // A pending selection waits for the tree; a gone one hands the URL to the shown file.
    if (!file || (selected && selected !== file.file_id && !gone)) return;
    remember({ fileId: file.file_id });
    const [route, query] = window.location.hash.split("?");
    const params = new URLSearchParams(query);
    params.set("paper_file", file.file_id);
    window.history.replaceState(
      window.history.state,
      "",
      `${window.location.pathname}${window.location.search}${route || "#"}?${params}`,
    );
  }, [file, selected, gone, remember]);
  const perform = async (action: () => Promise<void>) => {
    setActionError(null);
    try {
      await action();
    } catch (error) {
      setActionError({ error: String(error), retry: action });
    }
  };
  const git = usePaperGit(apiBase, `${tree?.revision}`, refresh);
  const gitLinked = git.status?.state === "linked";
  const [sideTab, setSideTab] = useState<"files" | "changes">("files");
  const [diffPath, setDiffPath] = useState<string | null>(null);
  const changesTab = gitLinked && sideTab === "changes";
  const [coachDiff, setCoachDiff] = useState<CoachDiff | null>(null);
  const turnDiff = phone ? null : coachDiff;
  const shownDiff = gitLinked && !turnDiff ? diffPath : null;
  const changeCount = git.files.length;
  const sideTabs = gitLinked ? (
    <div className="segmented paper-grow paper-side-tabs" role="group" aria-label="Sidebar">
      {(["files", "changes"] as const).map((tab) => (
        <button key={tab} aria-pressed={sideTab === tab} onClick={() => setSideTab(tab)}>
          {tab === "files" ? "Files" : "Changes"}
          {tab === "changes" && changeCount > 0 && (
            <span className="paper-tab-count">{changeCount}</span>
          )}
        </button>
      ))}
    </div>
  ) : undefined;
  const fold = () => {
    setFolded(true);
    setFilesOpen(false);
    writePref(FOLD_KEY, "true");
  };
  const unfold = () => {
    setFolded(false);
    setFilesOpen(true);
    writePref(FOLD_KEY, "false");
  };
  // Focus follows an explicit request (Write, a picked or new file), never a load.
  const focusPending = useRef<string | null>(null);
  const focusReady = useCallback((id: string) => {
    const pane = writingPane.current;
    if (focusPending.current !== id || pane?.dataset.file !== id) return;
    const editor = pane.querySelector<HTMLElement>(".cm-editor");
    const view = editor && EditorView.findFromDOM(editor);
    if (!view) return;
    focusPending.current = null;
    view.focus();
  }, []);
  const focusEditor = (id = file?.file_id) => {
    if (!id) return;
    focusPending.current = id;
    if (document?.key.file === id) requestAnimationFrame(() => focusReady(id));
  };
  const switchView = (next: typeof view, id = file?.file_id) => {
    setView(next);
    if (next !== "write") {
      focusPending.current = null;
      setRight(next === "refs" ? "reference" : next);
    } else focusEditor(id);
  };
  const openReference = (key: string) => {
    const entry = findReference(references.entries, { key });
    setReference({ key, fileId: entry?.file.file_id });
    setRight("reference");
    setView("refs");
  };
  const chooseReference = (entry: PaperReference) => {
    setReference({ key: entry.key, fileId: entry.file.file_id });
    if (phone) return;
    setRight("reference");
    const dom = writingPane.current?.querySelector<HTMLElement>(".cm-editor");
    const editor = dom && EditorView.findFromDOM(dom);
    const insert = citation(entry.key, file?.format ?? "");
    if (!entry.unreadable && editor && insert && !document?.getSnapshot().closed) {
      editor.dispatch(editor.state.replaceSelection(insert));
      editor.focus();
    }
  };
  const locate = (next: PaperFocus) => {
    setSelected(next.file_id);
    setFocus(next);
    setView("write");
  };
  const locateSpot = (spot: PreviewSourceSpot | null) => {
    const source = spot && tree?.files.find((item) => item.path === spot.path);
    if (spot && source) locate({ kind: "source", file_id: source.file_id, spot });
  };
  const locateSource = (event: React.MouseEvent) =>
    locateSpot(previewSourceSpot(event.target as Element, window.getSelection(), event.clientY));
  const previewActive = right === "preview" && (!phone || view === "preview");
  const paperComments = usePaperComments({
    projectId: project.id,
    file,
    subscribe: document?.subscribe,
    editorPane: writingPane,
    previewPane,
    sourceVisible: Boolean(document) && !shownDiff && !turnDiff && (!phone || view === "write"),
    previewVisible: previewActive,
    showSource: () => switchView("write"),
    showPreview: () => switchView("preview"),
    send: sendThreads,
    canPost: true,
  });
  // Mounted on first view, then kept while hidden so its PDF and position survive.
  const [previewShown, setPreviewShown] = useState(previewActive);
  if (previewActive && !previewShown) setPreviewShown(true);
  const retryError = actionError?.error ?? identityError ?? resource.error ?? null;
  const split = usePaperSplit();
  const filesSplit = usePaperFilesSplit(!folded);
  return (
    <section
      className={`paper-live-workspace ${folded ? "files-folded" : ""} ${filesOpen ? "files-open" : ""} ${filesSplit.sized ? "files-sized" : ""} ${paperComments.railOpen && file ? "comments-open" : ""} ${retryError ? "paper-failed" : ""}`}
      data-view={view}
      style={{ ...split.style, ...filesSplit.style }}
    >
      <div className="paper-files-column" id="paper-files-pane" ref={filesSplit.files}>
        {filesSplit.handle && (
          <ResizeHandle
            label="Resize files"
            controls="paper-files-pane paper-editor-pane"
            className="paper-split-handle paper-files-handle"
            {...filesSplit.handle}
          />
        )}
        {changesTab ? (
          <aside className="paper-box paper-files paper-git-side">
            <header className="paper-bar">
              {sideTabs}
              <button className="icon-button" aria-label="Fold files" onClick={fold}>
                <PanelLeft size={16} />
              </button>
            </header>
            <PaperGitChanges
              git={git}
              openPath={shownDiff}
              onOpenDiff={(path) => {
                setDiffPath(path);
                setFilesOpen(false);
                switchView("write");
              }}
            />
          </aside>
        ) : (
          <PaperFiles
            title={sideTabs}
            apiBase={apiBase}
            projectId={project.id}
            files={tree?.files ?? []}
            selected={file?.file_id ?? null}
            onSelect={(id, existsAt) => {
              setDiffPath(null);
              setSelected(id, existsAt);
              setFilesOpen(false);
              switchView("write", id);
            }}
            onFold={fold}
            onChanged={refresh}
            exportControl={phone ? <PaperExport projectId={project.id} file={file} /> : undefined}
          />
        )}
        {!phone && <PaperReferences {...references} folded onChoose={chooseReference} />}
      </div>
      <main className="paper-box paper-editor-box" id="paper-editor-pane" ref={split.editor}>
        <header className="paper-bar paper-document-header">
          <button className="icon-button paper-show-files" aria-label="Show files" onClick={unfold}>
            <PanelLeft size={16} />
          </button>
          <strong className="paper-path" title={shownDiff ?? file?.path}>
            {shownDiff ?? file?.path ?? "Paper"}
          </strong>
          {shownDiff ? (
            <button
              className="icon-button"
              aria-label="Close diff"
              onClick={() => setDiffPath(null)}
            >
              <X size={16} />
            </button>
          ) : (
            file && <span className="paper-format">{file.format}</span>
          )}
          <span className="paper-grow" />
          {strandedElsewhere && (
            <button
              className="icon-button paper-failed"
              aria-label="Open unsent file"
              title={strandedElsewhere.path}
              onClick={() => {
                setSelected(strandedElsewhere.file_id);
                switchView("write", strandedElsewhere.file_id);
              }}
            >
              <FileWarning size={16} />
            </button>
          )}
          {document && (
            <DocumentStatus
              owner={document}
              fileName={file!.path.split("/").at(-1)!}
              checking={checking}
              recover={
                stranded
                  ? async () => {
                      const body = new FormData();
                      const taken = new Set(tree!.files.map((item) => item.path));
                      let path = file!.path;
                      for (let n = 1; taken.has(path); n++)
                        path = file!.path.replace(
                          /(\.[^./]+)?$/,
                          ` (recovered${n > 1 ? ` ${n}` : ""})$1`,
                        );
                      body.append("path", path);
                      body.append(
                        "file",
                        new Blob([document.getSnapshot().text], { type: "text/plain" }),
                        path.split("/").at(-1),
                      );
                      const editor = writingPane.current?.querySelector<HTMLElement>(".cm-editor");
                      const cursor = editor && EditorView.findFromDOM(editor)?.state.selection.main;
                      const receipt = await api<{ file_ids: string[]; revision: number }>(
                        `${apiBase}/paper/uploads`,
                        { method: "POST", body },
                      );
                      const [created] = receipt.file_ids;
                      // The new file holds this text now; it opens where the writer was.
                      if (cursor) carryPaperSelection({ ...document.key, file: created }, cursor);
                      focusPending.current = created;
                      // Select first: a render between the two would find the deleted file
                      // neither listed nor stranded and open another one.
                      flushSync(() => setSelected(created, receipt.revision));
                      document.discard();
                      refresh();
                    }
                  : undefined
              }
            />
          )}
          {retryError && (
            <button
              className="icon-button"
              aria-label="Retry Paper"
              title={String(retryError)}
              onClick={() => {
                if (actionError) void perform(actionError.retry);
                else if (identityError) setIdentityRetry((value) => value + 1);
                else refresh();
              }}
            >
              <RotateCcw size={16} />
            </button>
          )}
          {file && (
            <button
              className="icon-button paper-comments-button"
              aria-label="Comments"
              aria-pressed={paperComments.railOpen}
              onClick={() => paperComments.setRailOpen(!paperComments.railOpen)}
            >
              <MessageSquare size={16} />
              {paperComments.openCount > 0 && (
                <span className="paper-comment-count" aria-hidden="true">
                  {paperComments.openCount}
                </span>
              )}
            </button>
          )}
          <PaperGitPill git={git} />
          <button
            className="icon-button paper-history-button"
            aria-label="Paper history"
            onClick={() => setHistory(true)}
          >
            <History size={16} />
          </button>
          {!phone && <PaperExport projectId={project.id} file={file} />}
        </header>
        <div className="paper-phone-segments">
          <div className="segmented" role="group" aria-label="Paper view">
            {(["write", "preview", "coach", "refs"] as const).map((panel) => (
              <button
                key={panel}
                aria-pressed={view === panel}
                onClick={() => {
                  if (panel === "refs") setReference(null);
                  switchView(panel);
                }}
              >
                {panel[0].toUpperCase() + panel.slice(1)}
              </button>
            ))}
          </div>
          {phone && <span className="paper-panel-bar" ref={setPanelBar} />}
        </div>
        {shownDiff && <PaperGitDiff git={git} path={shownDiff} refreshKey={`${tree?.revision}`} />}
        {turnDiff && (
          <PaperTurnDiff
            key={`${turnDiff.operationId}:${turnDiff.path}`}
            projectId={project.id}
            apiBase={apiBase}
            chatId={turnDiff.chatId}
            operationId={turnDiff.operationId}
            path={turnDiff.path}
            send={sendThreads}
            canPost
            onClose={() => setCoachDiff(null)}
          />
        )}
        <div
          className="paper-writing-pane"
          ref={writingPane}
          data-file={document?.key.file}
          style={shownDiff || turnDiff ? { display: "none" } : undefined}
        >
          {document ? (
            <LiveEditor
              owner={document}
              references={references.entries}
              onReference={openReference}
              onReady={focusReady}
              maxBytes={tree?.max_doc_bytes}
              format={file!.format}
              changes={NO_CHANGES}
              focus={focus}
              onDecide={() => undefined}
              onCursorLine={(line) => setCursor({ file: file!.file_id, line })}
            />
          ) : file?.kind === "binary" ? (
            <a
              className="paper-binary-link"
              download
              href={`${apiBase}/paper/files/${encodeURIComponent(file.file_id)}/raw`}
            >
              {file.path}
            </a>
          ) : !tree || !identity ? (
            <LoaderCircle size={18} className="spin" aria-label="Loading Paper" />
          ) : null}
        </div>
      </main>
      {split.handle && (
        <ResizeHandle
          label="Resize editor"
          controls="paper-editor-pane paper-right-pane"
          className="paper-split-handle"
          {...split.handle}
        />
      )}
      <aside className="paper-box paper-right-box" id="paper-right-pane" ref={split.right}>
        <header className="paper-bar paper-desktop-segments">
          <div className="segmented" role="group" aria-label="Paper panel">
            {(["preview", "coach", "reference"] as const).map((panel) => (
              <button
                key={panel}
                aria-pressed={right === panel}
                onClick={() => switchView(panel === "reference" ? "refs" : panel)}
              >
                {panel[0].toUpperCase() + panel.slice(1)}
              </button>
            ))}
          </div>
          {!phone && <span className="paper-panel-bar" ref={setPanelBar} />}
        </header>
        <div
          className="paper-right-pane"
          hidden={right !== "preview"}
          ref={previewPane}
          tabIndex={-1}
          onDoubleClick={locateSource}
        >
          {previewShown &&
            file &&
            tree &&
            (document ? (
              <LivePreview
                owner={document}
                projectId={project.id}
                file={file}
                tree={tree}
                onOpenFile={setSelected}
                active={previewActive}
                toolbar={(phone ? view : right) === "preview" ? panelBar : null}
                cursorLine={cursor.file === file.file_id ? cursor.line : 1}
                onAnchor={paperComments.openAnchor}
                onAnchorCancel={paperComments.cancelAnchor}
                onSourceSpot={locateSpot}
                onSelectionOffer={paperComments.offerFrameSelection}
              />
            ) : (
              <PaperPreview
                projectId={project.id}
                file={file}
                tree={tree}
                text=""
                onOpenFile={setSelected}
                active={previewActive}
                toolbar={(phone ? view : right) === "preview" ? panelBar : null}
                cursorLine={cursor.file === file.file_id ? cursor.line : 1}
                onAnchor={paperComments.openAnchor}
                onAnchorCancel={paperComments.cancelAnchor}
                onSourceSpot={locateSpot}
                onSelectionOffer={paperComments.offerFrameSelection}
              />
            ))}
        </div>
        <div className="paper-right-pane" hidden={right !== "reference"}>
          {phone && !reference ? (
            <PaperReferences {...references} onChoose={chooseReference} />
          ) : (
            <PaperReferencePage
              entry={referenceEntry}
              unknown={reference?.key ?? null}
              onBack={phone ? () => setReference(null) : undefined}
              onSource={(entry) =>
                locate({ kind: "reference", file_id: entry.file.file_id, from: entry.from })
              }
            />
          )}
        </div>
        <div className="paper-right-pane" hidden={right !== "coach"}>
          <PaperCoach
            apiBase={apiBase}
            project={project}
            tasks={tasks}
            onStartTask={onStartTask}
            actions={coachActions}
            toolbar={(phone ? view : right) === "coach" ? panelBar : null}
            threadSender={coachThreads}
            opening={editorState?.coach}
            onOpening={rememberCoach}
            diff={coachDiff}
            onDiff={setCoachDiff}
            inlineDiff={phone}
          />
        </div>
      </aside>
      {paperComments.layers}
      {history && tree && (
        <PaperHistory
          apiBase={apiBase}
          tree={tree}
          onClose={() => setHistory(false)}
          onRestored={refresh}
        />
      )}
    </section>
  );
}
function DocumentStatus({
  owner,
  fileName,
  checking,
  recover,
}: {
  owner: PaperDocument;
  fileName: string;
  checking: boolean;
  recover?: () => Promise<void>;
}) {
  const state = useSyncExternalStore(owner.subscribe, owner.getSnapshot);
  const [recovering, setRecovering] = useState(false);
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  useEffect(() => {
    if (state.saved) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [state.saved]);
  return (
    <>
      <div className="paper-presence" aria-label="People editing">
        {state.presence.map((person) => (
          <span
            key={person.id}
            title={person.name}
            className="paper-avatar"
            style={{ backgroundColor: person.color }}
          >
            {person.name
              .split(/\s+/)
              .map((part) => part[0])
              .slice(0, 2)
              .join("")}
          </span>
        ))}
      </div>
      <span
        className="visually-hidden"
        data-testid="paper-saved"
        data-saved={state.saved}
        role="status"
      >
        {state.saved ? "Saved" : "Saving"}
      </span>
      {state.closed !== null && !state.saved && !recover && (
        <button
          className="icon-button"
          aria-label="Download unsent text"
          onClick={() => {
            const link = window.document.createElement("a");
            link.href = URL.createObjectURL(
              new Blob([owner.getSnapshot().text], { type: "text/plain" }),
            );
            link.download = fileName;
            link.click();
            setTimeout(() => URL.revokeObjectURL(link.href), 0);
          }}
        >
          <Download size={16} />
        </button>
      )}
      {((state.error && !(checking && state.closed === 4403)) || recover) && (
        <button
          className="icon-button paper-failed"
          aria-label={recover ? "Recreate file" : "Reconnect Paper"}
          title={recoveryError ?? state.error ?? undefined}
          disabled={recovering}
          onClick={async () => {
            setRecovering(true);
            setRecoveryError(null);
            try {
              await (recover ? recover() : owner.reconnect());
            } catch (error) {
              setRecoveryError(String(error));
            } finally {
              setRecovering(false);
            }
          }}
        >
          <RotateCcw size={16} />
        </button>
      )}
    </>
  );
}
function LiveEditor({
  onReady,
  ...props
}: React.ComponentProps<typeof PaperEditor> & { onReady: (id: string) => void }) {
  const state = useSyncExternalStore(props.owner.subscribe, props.owner.getSnapshot);
  useEffect(() => {
    if (!state.ready) return;
    const frame = requestAnimationFrame(() => onReady(props.owner.key.file));
    return () => cancelAnimationFrame(frame);
  }, [props.owner, state.ready, onReady]);
  return state.ready ? (
    <PaperEditor {...props} />
  ) : (
    <LoaderCircle className="spin" size={18} aria-label="Loading document" />
  );
}
function LivePreview({
  owner,
  ...props
}: Omit<React.ComponentProps<typeof PaperPreview>, "text"> & { owner: PaperDocument }) {
  const state = useSyncExternalStore(owner.subscribe, owner.getSnapshot);
  return <PaperPreview {...props} text={state.text} />;
}
