import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Cloud, CloudOff, Copy, History, LoaderCircle, PanelLeft, RotateCcw } from "lucide-react";
import { api } from "../core/api";
import { referenceUrl } from "../core/projectReferences";
import type {
  AgentTask,
  GraphTargetRef,
  IdentityResponse,
  PaperFile,
  ProjectSnapshot,
  PublicHealth,
  StartAgentTask,
} from "../core/types";
import { useRefreshRuntime, useResource } from "../core/useResource";
import { resourceKeyId } from "../core/resourceStore";
import { paperProfile } from "./paperResources";
import { acquirePaperDocument, observePaperBackend, type PaperDocument } from "./paperDocuments";
import { PaperEditor } from "./PaperEditor";
import { PaperFiles } from "./PaperFiles";
import { PaperPreview } from "./PaperPreview";
import { PaperCoach } from "./PaperCoach";
import { PaperHistory } from "./PaperHistory";
import { PaperDialog } from "./PaperDialog";
import { usePaperChanges, type PaperChange, type PaperChangeAction } from "./PaperChanges";

/** Route snapshots retain selection only; IndexedDB owns all document content. */
export interface PaperEditorState {
  fileId?: string;
}
interface Props {
  graphTarget?: GraphTargetRef;
  apiBase: string;
  project: ProjectSnapshot;
  owner?: string;
  editorState?: PaperEditorState;
  requestedFileId?: { fileId: string | null };
  onEditorStateChange?: (state: PaperEditorState) => void;
  tasks: AgentTask[];
  onStartTask: StartAgentTask;
}
function storedFold() {
  try {
    return localStorage.getItem("rcp:paper-files-folded") === "true";
  } catch {
    return false;
  }
}
export function LoadedPaperWorkspace(props: Props) {
  return <PaperWorkspace {...props} />;
}
export function PaperWorkspace({
  apiBase,
  project,
  owner: resourceOwner,
  editorState,
  requestedFileId,
  onEditorStateChange,
  tasks,
  onStartTask,
}: Props) {
  const { store } = useRefreshRuntime();
  const profile = paperProfile(project.id, apiBase);
  const resource = useResource(resourceOwner ?? `${project.id}:paper-view`, profile);
  const tree = resource.value?.tree;
  const [identity, setIdentity] = useState<IdentityResponse | null>(null);
  const [identityError, setIdentityError] = useState<string | null>(null);
  const [identityRetry, setIdentityRetry] = useState(0);
  const [selected, setSelected] = useState<string | null>(
    requestedFileId?.fileId ??
      new URLSearchParams(window.location.hash.split("?")[1]).get("paper_file") ??
      editorState?.fileId ??
      null,
  );
  useEffect(() => {
    if (requestedFileId?.fileId) setSelected(requestedFileId.fileId);
  }, [requestedFileId]);
  const [folded, setFolded] = useState(storedFold);
  const [filesOpen, setFilesOpen] = useState(false);
  const [view, setView] = useState<"write" | "preview" | "coach">("write");
  const [right, setRight] = useState<"preview" | "coach">("preview");
  const [phone, setPhone] = useState(() => window.matchMedia("(max-width: 700px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 700px)");
    const changed = () => setPhone(media.matches);
    media.addEventListener("change", changed);
    return () => media.removeEventListener("change", changed);
  }, []);
  const [history, setHistory] = useState(false);
  const [focusChange, setFocusChange] = useState<PaperChange | null>(null);
  const [actionError, setActionError] = useState<{
    error: string;
    retry: () => Promise<void>;
  } | null>(null);
  const [resolving, setResolving] = useState(false);
  const changeFeed = usePaperChanges(
    apiBase,
    `${tree?.revision}:${tasks.map((task) => `${task.operation_id}:${task.updated_at}`).join("|")}`,
  );
  const refresh = useCallback(
    () => store.invalidate(resourceKeyId(paperProfile(project.id, apiBase).key)),
    [store, project.id, apiBase],
  );
  useEffect(() => {
    const controller = new AbortController();
    setIdentity(null);
    void Promise.all([
      api<IdentityResponse>("/api/identity", { signal: controller.signal }),
      api<PublicHealth>("/api/health", { signal: controller.signal }),
    ])
      .then(([value, health]) => {
        if (controller.signal.aborted) return;
        observePaperBackend({ ok: true, health, message: null, reason: "paper-open" });
        setIdentity(value);
        setIdentityError(null);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setIdentityError(String(error));
      });
    return () => controller.abort();
  }, [apiBase, identityRetry]);
  useEffect(() => {
    const reset = () => {
      setIdentity(null);
      setIdentityRetry((value) => value + 1);
    };
    window.addEventListener("rcp:paper-backend-changed", reset);
    window.addEventListener("rcp:session-required", reset);
    window.addEventListener("rcp:read-access-lost", reset);
    return () => {
      window.removeEventListener("rcp:paper-backend-changed", reset);
      window.removeEventListener("rcp:session-required", reset);
      window.removeEventListener("rcp:read-access-lost", reset);
    };
  }, []);
  const opened = useRef<{ file: PaperFile; document: PaperDocument | null } | null>(null);
  const last = opened.current;
  const current = selected ?? last?.file.file_id;
  const listed = tree?.files.find((item) => item.file_id === current);
  // A deleted or inaccessible file stays open, read-only, while it holds unsent local text.
  const stranded =
    !listed && last?.file.file_id === current && last?.document?.getSnapshot().saved === false
      ? last.file
      : undefined;
  const file = listed ?? stranded ?? tree?.files[0];
  const document = useMemo(
    () =>
      identity && file?.kind === "text"
        ? acquirePaperDocument(
            {
              backend: `${new URL(apiBase, window.location.href).origin}:${identity.space_id}`,
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
  }, [file, document]);
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
        if (lastClosed === 4403) refresh();
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
  }, [document, refresh]);
  useEffect(() => {
    if (file) onEditorStateChange?.({ fileId: file.file_id });
  }, [file, onEditorStateChange]);
  const perform = async (action: () => Promise<void>) => {
    setActionError(null);
    try {
      await action();
    } catch (error) {
      setActionError({ error: String(error), retry: action });
    }
  };
  const act = async (id: string, action: PaperChangeAction) => {
    try {
      await changeFeed.act(id, action);
    } finally {
      refresh();
    }
  };
  const fold = () => {
    setFolded(true);
    setFilesOpen(false);
    try {
      localStorage.setItem("rcp:paper-files-folded", "true");
    } catch {
      /* Device preference is optional. */
    }
  };
  const unfold = () => {
    setFolded(false);
    setFilesOpen(true);
    try {
      localStorage.setItem("rcp:paper-files-folded", "false");
    } catch {
      /* Device preference is optional. */
    }
  };
  const switchView = (next: typeof view) => {
    setView(next);
    if (next !== "write") setRight(next);
  };
  const locate = (change: PaperChange) => {
    setSelected(change.file_id);
    setFocusChange(change);
    setView("write");
  };
  const previewActive = right === "preview" && (!phone || view === "preview");
  // Mounted on first view, then kept while hidden so its PDF and position survive.
  const [previewShown, setPreviewShown] = useState(previewActive);
  if (previewActive && !previewShown) setPreviewShown(true);
  const retryError = actionError?.error ?? identityError ?? resource.error ?? changeFeed.error;
  return (
    <section
      className={`paper-live-workspace ${folded ? "files-folded" : ""} ${filesOpen ? "files-open" : ""} ${retryError ? "paper-failed" : ""}`}
      data-view={view}
    >
      <PaperFiles
        apiBase={apiBase}
        tree={
          tree ?? {
            revision: 0,
            saved_revision: 0,
            published_revision: 0,
            publish_state: "pending",
            hold: null,
            files: [],
          }
        }
        selected={file?.file_id ?? null}
        onSelect={(id) => {
          setSelected(id);
          setFilesOpen(false);
        }}
        onFold={fold}
        onChanged={refresh}
      />
      <main className="paper-box paper-editor-box">
        <header className="paper-bar paper-document-header">
          <button className="icon-button paper-show-files" aria-label="Show files" onClick={unfold}>
            <PanelLeft size={16} />
          </button>
          <strong className="paper-path" title={file?.path}>
            {file?.path ?? "Paper"}
          </strong>
          {file && <span className="paper-format">{file.format}</span>}
          <span className="paper-grow" />
          {document && <DocumentStatus owner={document} />}
          {retryError && (
            <button
              className="icon-button"
              aria-label="Retry Paper"
              title={String(retryError)}
              onClick={() => {
                if (actionError) void perform(actionError.retry);
                else if (identityError) setIdentityRetry((value) => value + 1);
                else {
                  refresh();
                  void changeFeed.refresh();
                }
              }}
            >
              <RotateCcw size={16} />
            </button>
          )}
          <button
            className="icon-button"
            aria-label="Paper history"
            onClick={() => setHistory(true)}
          >
            <History size={16} />
          </button>
          <button
            className="icon-button paper-copy"
            aria-label="Copy reference"
            disabled={!file}
            onClick={() =>
              void perform(async () => {
                await navigator.clipboard.writeText(
                  referenceUrl(
                    project.id,
                    { kind: "main" },
                    { kind: "paper", file_id: file!.file_id },
                  ),
                );
              })
            }
          >
            <Copy size={16} />
          </button>
        </header>
        <div className="paper-phone-segments paper-segments" aria-label="Paper view">
          {(["write", "preview", "coach"] as const).map((panel) => (
            <button key={panel} aria-pressed={view === panel} onClick={() => switchView(panel)}>
              {panel[0].toUpperCase() + panel.slice(1)}
            </button>
          ))}
        </div>
        <div className="paper-writing-pane">
          {document ? (
            <LiveEditor
              owner={document}
              format={file!.format}
              changes={changeFeed.changes}
              focusChange={focusChange}
              onDecide={(id, action) => void perform(() => act(id, action))}
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
      <aside className="paper-box paper-right-box">
        <header className="paper-bar paper-desktop-segments">
          <div className="paper-segments">
            <button
              aria-pressed={right === "preview"}
              onClick={() => {
                setRight("preview");
                setView("preview");
              }}
            >
              Preview
            </button>
            <button
              aria-pressed={right === "coach"}
              onClick={() => {
                setRight("coach");
                setView("coach");
              }}
            >
              Coach
            </button>
          </div>
        </header>
        <div className="paper-right-pane" hidden={right !== "preview"}>
          {previewShown &&
            file &&
            tree &&
            (document ? (
              <LivePreview
                owner={document}
                projectId={project.id}
                file={file}
                tree={tree}
                active={previewActive}
              />
            ) : (
              <PaperPreview
                projectId={project.id}
                file={file}
                tree={tree}
                text=""
                active={previewActive}
              />
            ))}
        </div>
        <div className="paper-right-pane" hidden={right !== "coach"}>
          <PaperCoach
            apiBase={apiBase}
            project={project}
            tasks={tasks}
            onStartTask={onStartTask}
            changes={changeFeed.changes}
            onChangeAction={act}
            onUndoAll={async (taskId) => {
              try {
                await changeFeed.undoAll(taskId);
              } finally {
                refresh();
              }
            }}
            onLocate={locate}
          />
        </div>
      </aside>
      {history && tree && (
        <PaperHistory
          apiBase={apiBase}
          tree={tree}
          onClose={() => setHistory(false)}
          onRestored={refresh}
        />
      )}
      {tree?.hold && (
        <PaperDialog
          className={`paper-box paper-confirm ${actionError ? "paper-failed" : ""}`}
          label="Choose Paper version"
          onClose={() => {
            /* A version must be chosen. */
          }}
        >
          <strong>Choose Paper version</strong>
          <div className="paper-bar">
            {(["draft", "canonical"] as const).map((keep) => (
              <button
                key={keep}
                className="button"
                disabled={resolving}
                onClick={() =>
                  void perform(async () => {
                    setResolving(true);
                    try {
                      await api(`${apiBase}/paper/hold/resolve`, {
                        method: "POST",
                        body: JSON.stringify({ keep }),
                      });
                      refresh();
                    } finally {
                      setResolving(false);
                    }
                  })
                }
              >
                {keep === "draft" ? "Keep draft" : "Keep saved"}
              </button>
            ))}
          </div>
          {actionError && (
            <button
              className="icon-button"
              aria-label="Retry choice"
              onClick={() => void perform(actionError.retry)}
            >
              <RotateCcw size={16} />
            </button>
          )}
        </PaperDialog>
      )}
    </section>
  );
}
function DocumentStatus({ owner }: { owner: PaperDocument }) {
  const state = useSyncExternalStore(owner.subscribe, owner.getSnapshot);
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
        className={`paper-saved ${state.saved ? "saved" : ""}`}
        data-testid="paper-saved"
        data-saved={state.saved}
        role="status"
        aria-label={state.saved ? "Saved" : "Saving"}
        title={state.saved ? "Saved" : "Saving"}
      >
        {state.connected ? <Cloud size={16} /> : <CloudOff size={16} />}
      </span>
      {state.error && (
        <button
          className="icon-button paper-failed"
          aria-label="Reconnect Paper"
          title={state.error}
          onClick={() => owner.reconnect()}
        >
          <RotateCcw size={16} />
        </button>
      )}
    </>
  );
}
function LiveEditor(props: React.ComponentProps<typeof PaperEditor>) {
  const state = useSyncExternalStore(props.owner.subscribe, props.owner.getSnapshot);
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
