import { CopyReferenceButton } from "../core/CopyReferenceButton";
import { MAIN_GRAPH } from "../core/graphTarget";
import type { GraphTargetRef } from "../core/types";
import {
  TriangleAlert,
  Check,
  ChevronRight,
  Pencil,
  History,
  LoaderCircle,
  MessageCirclePlus,
  Send,
  WifiOff,
  RotateCcw,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { isActiveTask, reconstructTaskTranscript, relatedCoachTasks } from "../agents/agentTasks";
import { useRefreshRuntime, useResource } from "../core/useResource";
import { resourceKeyId } from "../core/resourceStore";
import { createPaperDraft, paperProfile, savePaperDraft } from "./paperResources";
import { MarkdownAnswer } from "../core/chatMarkdown";
import { profileRunConfig } from "../core/AgentConfigControls";
import { SkillPicker, useSkillPicker } from "../core/SkillPicker";
import { EMPTY_SKILL_SELECTION, skillInvocationFields } from "../core/skillPickerModel";
import type {
  AgentRunConfig,
  AgentTask,
  PaperSnapshot,
  ProjectSnapshot,
  StartAgentTask,
  WritingSession,
} from "../core/types";

export interface PaperEditorState {
  content: string;
  draftVersion: string | null;
  incomingContent: string;
  incomingCanonicalHash: string | null;
  saveBaseHash: string | null;
  dirty: boolean;
  saveError: string | null;
  buffersSwapped: boolean;
  message: string;
}

interface Props {
  graphTarget?: GraphTargetRef;
  apiBase: string;
  project: ProjectSnapshot;
  initialPaper: PaperSnapshot;
  owner?: string;
  editorState?: PaperEditorState;
  onEditorStateChange?: (state: PaperEditorState) => void;
  stale?: boolean;
  sessions?: WritingSession[];
  onCreate: () => Promise<PaperSnapshot>;
  onSave: (
    content: string,
    baseHash: string | null,
    version: string | null,
  ) => Promise<PaperSnapshot>;
  tasks: AgentTask[];
  onStartTask: StartAgentTask;
}

const MIN_EDITOR_SHARE = 35;
const MAX_EDITOR_SHARE = 70;
const MIN_EDITOR_WIDTH = 360;
const MIN_COACH_WIDTH = 320;
const DIVIDER_WIDTH = 9;
const PAPER_VIEW_STORAGE_PREFIX = "rcp:paper-view";

type PaperView = "write" | "preview" | "incoming";

interface EditorShareBounds {
  minimum: number;
  maximum: number;
}

function editorShareBoundsForWidth(width: number): EditorShareBounds {
  if (!width) return { minimum: MIN_EDITOR_SHARE, maximum: MAX_EDITOR_SHARE };
  const minimum = Math.min(
    MAX_EDITOR_SHARE,
    Math.max(MIN_EDITOR_SHARE, (MIN_EDITOR_WIDTH / width) * 100),
  );
  const coachConstrainedMaximum = Math.min(
    MAX_EDITOR_SHARE,
    ((width - DIVIDER_WIDTH - MIN_COACH_WIDTH) / width) * 100,
  );
  return { minimum, maximum: Math.max(minimum, coachConstrainedMaximum) };
}

function clampEditorShare(value: number, bounds: EditorShareBounds): number {
  return Math.min(bounds.maximum, Math.max(bounds.minimum, value));
}

function storedPaperView(projectId: string, allowIncoming: boolean): PaperView {
  try {
    const stored = localStorage.getItem(`${PAPER_VIEW_STORAGE_PREFIX}:${projectId}`);
    if (stored === "preview" || (allowIncoming && stored === "incoming")) return stored;
    return "write";
  } catch {
    return "write";
  }
}

export function swapPaperBuffers(editor: string, incoming: string): [string, string] {
  return [incoming, editor];
}

/** Paper uses the same resource lifetime and refresh rules as every other view. */
export function LoadedPaperWorkspace(props: Omit<Props, "initialPaper" | "onSave" | "onCreate">) {
  const { store } = useRefreshRuntime();
  const profile = paperProfile(props.project.id, props.apiBase);
  const id = resourceKeyId(profile.key);
  const view = useResource(props.owner ?? `${props.project.id}:paper-view`, profile);
  if (!view.value || (view.stale && view.value.paper.sync_state === "not_created")) {
    return (
      <section
        className="paper-empty"
        aria-busy={view.loading}
        style={view.error ? { outline: "1px solid var(--danger, #c33)" } : undefined}
      >
        {view.error ? (
          <button
            className="icon-button"
            aria-label="Retry Paper"
            onClick={() => store.invalidate(id)}
          >
            <RotateCcw size={16} />
          </button>
        ) : (
          <LoaderCircle className="spin" size={16} aria-label="Loading Paper" />
        )}
      </section>
    );
  }
  return (
    <PaperWorkspace
      {...props}
      initialPaper={view.value.paper}
      sessions={view.value.sessions}
      stale={view.stale}
      onCreate={() => createPaperDraft(store, profile)}
      onSave={(content, baseHash, version) =>
        savePaperDraft(store, profile, content, baseHash, version)
      }
    />
  );
}

export function PaperWorkspace({
  project,
  initialPaper,
  graphTarget = MAIN_GRAPH,
  tasks,
  onStartTask,
  editorState,
  onEditorStateChange,
  stale = false,
  sessions = [],
  onCreate,
  onSave,
}: Props) {
  const paper = initialPaper;
  const [draftVersion, setDraftVersion] = useState(
    editorState ? editorState.draftVersion : initialPaper.draft_version,
  );
  const [content, setContent] = useState(editorState?.content ?? initialPaper.content);
  const [incomingContent, setIncomingContent] = useState(
    editorState?.incomingContent ?? initialPaper.incoming_content ?? "",
  );
  // Keep the version paired with the visible Incoming buffer. Canonical may
  // advance again while that buffer temporarily holds the displaced draft.
  const [incomingCanonicalHash, setIncomingCanonicalHash] = useState(
    editorState
      ? editorState.incomingCanonicalHash
      : initialPaper.sync_state === "behind"
        ? (initialPaper.canonical_hash ?? null)
        : null,
  );
  const [saveBaseHash, setSaveBaseHash] = useState(
    editorState ? editorState.saveBaseHash : (initialPaper.base_hash ?? null),
  );
  const [dirty, setDirty] = useState(editorState?.dirty ?? false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(editorState?.saveError ?? null);
  const [creating, setCreating] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const activeSession =
    sessions.find((session) => session.native_session_id === activeSessionId) ?? null;
  const [freshSession, setFreshSession] = useState(false);
  const [pendingCoachTaskId, setPendingCoachTaskId] = useState<string | null>(null);
  const [coachSubmitting, setCoachSubmitting] = useState(false);
  const [config, setConfig] = useState<AgentRunConfig>(() =>
    profileRunConfig(project.agent_profiles.paper_coach),
  );
  const [message, setMessage] = useState(editorState?.message ?? "");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [paperView, setPaperView] = useState<PaperView>(() =>
    storedPaperView(project.id, initialPaper.sync_state === "behind"),
  );
  const [editorShare, setEditorShare] = useState(62);
  const [editorShareBounds, setEditorShareBounds] = useState<EditorShareBounds>({
    minimum: MIN_EDITOR_SHARE,
    maximum: MAX_EDITOR_SHARE,
  });
  const workspace = useRef<HTMLElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const coachTextarea = useRef<HTMLTextAreaElement>(null);
  const latestContent = useRef(content);
  const buffersSwapped = useRef(editorState?.buffersSwapped ?? false);
  const paperSaveGeneration = useRef(0);

  useEffect(() => {
    onEditorStateChange?.({
      content,
      draftVersion,
      incomingContent,
      incomingCanonicalHash,
      saveBaseHash,
      dirty,
      saveError,
      buffersSwapped: buffersSwapped.current,
      message,
    });
  }, [
    content,
    draftVersion,
    incomingContent,
    incomingCanonicalHash,
    saveBaseHash,
    dirty,
    saveError,
    message,
    onEditorStateChange,
  ]);

  useEffect(() => {
    latestContent.current = content;
  }, [content]);

  useEffect(() => {
    if (!buffersSwapped.current) {
      setIncomingContent(paper.incoming_content ?? "");
      setIncomingCanonicalHash(
        paper.sync_state === "behind" ? (paper.canonical_hash ?? null) : null,
      );
    }
    // The editor's draft revision stays paired with its buffer, including after a conflict.
    if (paper.sync_state !== "behind") setSaveBaseHash(paper.base_hash ?? null);
  }, [paper]);

  const attachSession = (session: WritingSession) => {
    setActiveSessionId(session.native_session_id);
    setFreshSession(false);
    setPendingCoachTaskId(null);
    setConfig({
      provider: session.provider,
      model: session.model === "provider-default" ? "" : session.model,
      reasoning: session.reasoning ?? "medium",
      run_on: session.execution_machine,
    });
    setSubmitError(null);
  };

  useEffect(() => {
    if (freshSession) return;
    const sessionId = pendingCoachTaskId
      ? tasks.find((task) => task.operation_id === pendingCoachTaskId)?.native_session_id
      : (activeSession?.native_session_id ??
        tasks.find((task) => task.kind === "paper_coach" && task.native_session_id)
          ?.native_session_id);
    const session = sessions.find((item) => item.native_session_id === sessionId);
    if (session && session !== activeSession) attachSession(session);
  }, [sessions, tasks, freshSession, pendingCoachTaskId, activeSession]);

  const latestCoachTask = tasks.find((task) => task.kind === "paper_coach") ?? null;
  const pendingCoachTask = pendingCoachTaskId
    ? (tasks.find((task) => task.operation_id === pendingCoachTaskId) ?? null)
    : latestCoachTask && isActiveTask(latestCoachTask)
      ? latestCoachTask
      : null;
  const activeCoachTask = tasks.find(
    (task) =>
      task.kind === "paper_coach" &&
      isActiveTask(task) &&
      (activeSession
        ? task.native_session_id === activeSession.native_session_id
        : task.operation_id === pendingCoachTask?.operation_id),
  );
  const skillCatalog = project.skill_catalog ?? [];
  const skillDefaults = project.skill_defaults ?? EMPTY_SKILL_SELECTION;
  const readiness = project.provider_readiness[config.run_on]?.[config.provider];
  const skills = useSkillPicker({
    catalog: skillCatalog,
    defaults: skillDefaults,
    provider: config.provider,
    providerLabel: readiness?.label || config.provider,
    machine: config.run_on,
    inventory: project.provider_skill_inventories?.[config.run_on]?.[config.provider],
    message,
    onComplete: (next) => {
      setMessage(next);
      setSubmitError(null);
      window.requestAnimationFrame(() => {
        coachTextarea.current?.focus();
        coachTextarea.current?.setSelectionRange(next.length, next.length);
      });
    },
  });
  const resetSkills = skills.reset;

  useEffect(() => {
    resetSkills();
  }, [project.id, resetSkills]);

  useEffect(() => {
    if (!dirty || saving || saveError || paper.sync_state === "not_created") return;
    const timer = window.setTimeout(async () => {
      setSaving(true);
      const savedContent = latestContent.current;
      const generation = ++paperSaveGeneration.current;
      try {
        const next = await onSave(savedContent, saveBaseHash, draftVersion);
        if (generation !== paperSaveGeneration.current) return;
        paperSaveGeneration.current += 1;
        setDraftVersion(next.draft_version);
        setIncomingContent(next.incoming_content ?? "");
        setIncomingCanonicalHash(
          next.sync_state === "behind" ? (next.canonical_hash ?? null) : null,
        );
        if (next.sync_state !== "behind") setSaveBaseHash(next.base_hash ?? null);
        buffersSwapped.current = false;
        setDirty(next.content !== latestContent.current);
        setSaveError(null);
      } catch (error) {
        setSaveError(error instanceof Error ? error.message : String(error));
      } finally {
        setSaving(false);
      }
    }, 650);
    return () => window.clearTimeout(timer);
  }, [dirty, paper.sync_state, saveBaseHash, saveError, saving, draftVersion, onSave]);

  const create = async () => {
    if (creating) return;
    setCreating(true);
    setSaveError(null);
    try {
      const next = await onCreate();
      setDraftVersion(next.draft_version);
      setContent(next.content);
      setIncomingContent(next.incoming_content ?? "");
      setIncomingCanonicalHash(next.sync_state === "behind" ? (next.canonical_hash ?? null) : null);
      setSaveBaseHash(next.base_hash ?? null);
      buffersSwapped.current = false;
      latestContent.current = next.content;
      setDirty(next.sync_state === "unsynced");
      requestAnimationFrame(() => textarea.current?.focus());
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setCreating(false);
    }
  };

  const updateMessage = (next: string) => {
    setMessage(next);
    skills.readMessage(next);
    setSubmitError(null);
  };

  const send = async () => {
    const text = message.trim();
    if (!text || activeCoachTask || coachSubmitting) return;
    setSubmitError(null);
    setCoachSubmitting(true);
    try {
      const task = await onStartTask("paper_coach", {
        message: text,
        ...config,
        model: config.model || null,
        session_id: activeSession?.native_session_id ?? null,
        ...skillInvocationFields(skills.selection, skills.providerSkillNames),
      });
      setPendingCoachTaskId(task.operation_id);
      setMessage("");
      skills.reset();
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error));
    } finally {
      setCoachSubmitting(false);
    }
  };

  const sessionTasks = activeSession
    ? relatedCoachTasks(tasks, activeSession.native_session_id)
    : pendingCoachTask
      ? [pendingCoachTask]
      : [];
  const transcript = reconstructTaskTranscript(sessionTasks);
  const reviewStale = activeSession
    ? activeSession.graph_revision_examined < project.revision
    : false;
  const syncLabel = paper.sync_state.replace("_", " ");
  const wordCount = useMemo(
    () => (content.trim() ? content.trim().split(/\s+/).length : 0),
    [content],
  );
  const coachActive = Boolean(activeCoachTask);
  const constrainEditorShare = (value: number) => {
    const width = workspace.current?.getBoundingClientRect().width ?? 0;
    return clampEditorShare(value, editorShareBoundsForWidth(width));
  };
  const resizeFromPointer = (clientX: number) => {
    const bounds = workspace.current?.getBoundingClientRect();
    if (!bounds) return;
    setEditorShare(constrainEditorShare(((clientX - bounds.left) / bounds.width) * 100));
  };
  const selectPaperView = (next: PaperView) => {
    setPaperView(next);
    try {
      localStorage.setItem(`${PAPER_VIEW_STORAGE_PREFIX}:${project.id}`, next);
    } catch {}
  };
  const applyIncoming = () => {
    const [nextContent, nextIncoming] = swapPaperBuffers(content, incomingContent);
    setContent(nextContent);
    setIncomingContent(nextIncoming);
    latestContent.current = nextContent;
    setSaveBaseHash(incomingCanonicalHash);
    buffersSwapped.current = !buffersSwapped.current;
    setDirty(false);
    setSaveError(null);
  };

  useEffect(() => {
    if (paperView === "incoming") setSaveBaseHash(incomingCanonicalHash);
  }, [incomingCanonicalHash, paperView]);

  useEffect(() => {
    if (paper.sync_state === "behind" || paperView !== "incoming") return;
    setPaperView("write");
    try {
      localStorage.setItem(`${PAPER_VIEW_STORAGE_PREFIX}:${project.id}`, "write");
    } catch {}
  }, [paper.sync_state, paperView, project.id]);

  const paperCreated = paper.sync_state !== "not_created";
  useEffect(() => {
    const element = workspace.current;
    if (!element) return;

    const updateBounds = (width: number) => {
      const bounds = editorShareBoundsForWidth(width);
      setEditorShareBounds(bounds);
      setEditorShare((current) => clampEditorShare(current, bounds));
    };
    updateBounds(element.getBoundingClientRect().width);

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) updateBounds(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [paperCreated]);

  if (paper.sync_state === "not_created") {
    return (
      <section className="paper-empty">
        <div className="paper-sheet-preview">
          <span># Introduction</span>
          <i />
          <i />
          <i />
        </div>
        <div className="paper-empty-action">
          <button
            className="button primary"
            title={saveError ?? undefined}
            style={saveError ? { outline: "1px solid var(--danger, #c33)" } : undefined}
            disabled={creating || stale}
            onClick={() => void create()}
          >
            <Pencil size={16} /> {creating ? "Opening editor…" : "Create introduction"}
          </button>
        </div>
      </section>
    );
  }

  return (
    <section
      className="paper-workspace"
      aria-label={stale ? "Paper (stale)" : "Paper"}
      ref={workspace}
      style={{
        gridTemplateColumns: `minmax(${MIN_EDITOR_WIDTH}px, ${editorShare}%) ${DIVIDER_WIDTH}px minmax(${MIN_COACH_WIDTH}px, 1fr)`,
      }}
    >
      <div className="paper-editor-column">
        <header className="paper-toolbar">
          <div className="paper-view-controls">
            {paper.canonical_available && paper.canonical_hash && (
              <CopyReferenceButton
                projectId={project.id}
                graphTarget={graphTarget}
                reference={{ kind: "paper" }}
              />
            )}
            <div className="segmented" role="group" aria-label="Paper view">
              {(["write", "preview"] as const).map((view) => (
                <button
                  aria-pressed={paperView === view}
                  key={view}
                  onClick={() => selectPaperView(view)}
                  type="button"
                >
                  {view === "write" ? "Write" : "Preview"}
                </button>
              ))}
              {paper.sync_state === "behind" && (
                <button
                  aria-pressed={paperView === "incoming"}
                  onClick={() => selectPaperView("incoming")}
                  type="button"
                >
                  Incoming
                </button>
              )}
            </div>
            {paper.sync_state === "behind" && (
              <button
                aria-label="Swap editor and incoming introduction"
                className="button compact secondary paper-apply-incoming"
                disabled={saving}
                onClick={applyIncoming}
                type="button"
              >
                Apply
              </button>
            )}
          </div>
          <div className="paper-status">
            {stale && <WifiOff size={14} aria-label="Paper is stale" />}
            <span>{wordCount} words</span>
            <span className={`sync-state ${saveError ? "save-error" : paper.sync_state}`}>
              {!saveError && paper.sync_state === "synced" && <Check size={14} />}
              {!saveError && paper.sync_state === "unsynced" && <WifiOff size={14} />}
              {(saveError || paper.sync_state === "behind") && <TriangleAlert size={14} />}
              {saveError
                ? "save failed"
                : saving
                  ? "saving"
                  : dirty
                    ? "unsaved changes"
                    : syncLabel}
            </span>
          </div>
        </header>
        {saveError && (
          <button
            className="icon-button"
            aria-label="Retry save"
            title={saveError}
            style={{ outline: "1px solid var(--danger, #c33)" }}
            onClick={() => {
              setDraftVersion(paper.draft_version);
              setSaveError(null);
            }}
          >
            <RotateCcw size={16} />
          </button>
        )}
        {paperView === "write" ? (
          <textarea
            ref={textarea}
            className="markdown-editor"
            aria-label="Paper introduction Markdown"
            spellCheck
            value={content}
            onChange={(event) => {
              setContent(event.target.value);
              setDirty(true);
              setSaveError(null);
            }}
          />
        ) : (
          <article
            aria-label="Paper introduction preview"
            className="markdown-editor paper-markdown-preview chat-markdown"
          >
            <MarkdownAnswer text={paperView === "incoming" ? incomingContent : content} />
          </article>
        )}
      </div>

      <div
        aria-label="Resize paper editor and writing coach"
        aria-orientation="vertical"
        aria-valuemax={editorShareBounds.maximum}
        aria-valuemin={editorShareBounds.minimum}
        aria-valuenow={Math.round(editorShare)}
        className="paper-divider"
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
            event.preventDefault();
            setEditorShare((current) =>
              constrainEditorShare(current + (event.key === "ArrowLeft" ? -2 : 2)),
            );
          }
          if (event.key === "Home" || event.key === "End") {
            event.preventDefault();
            setEditorShare(
              constrainEditorShare(event.key === "Home" ? MIN_EDITOR_SHARE : MAX_EDITOR_SHARE),
            );
          }
        }}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          resizeFromPointer(event.clientX);
        }}
        onPointerMove={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId))
            resizeFromPointer(event.clientX);
        }}
        onPointerUp={(event) => {
          if (event.currentTarget.hasPointerCapture(event.pointerId)) {
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
        }}
        role="separator"
        tabIndex={0}
      />

      <aside className="coach-column">
        <div className="coach-history-bar">
          <details className="session-history">
            <summary>
              <History size={14} />
              <strong>Chat history</strong>
              {sessions.length > 0 && <small>{sessions.length}</small>}
            </summary>
            <div className="session-strip">
              {sessions.length === 0 ? (
                <span className="muted">No prior writing sessions</span>
              ) : (
                sessions.map((session) => (
                  <button
                    className={
                      activeSession?.native_session_id === session.native_session_id ? "active" : ""
                    }
                    key={session.native_session_id}
                    onClick={() => {
                      attachSession(session);
                    }}
                  >
                    <span>{session.title || "Untitled coach session"}</span>
                    <span className="session-meta">
                      {session.provider_label || session.provider} · rev{" "}
                      {session.graph_revision_examined}
                    </span>
                    <ChevronRight size={14} />
                  </button>
                ))
              )}
            </div>
          </details>
          <button
            className="icon-button compact coach-new-chat"
            type="button"
            title="New chat"
            aria-label="New chat"
            onClick={() => {
              setActiveSessionId(null);
              setFreshSession(true);
              setPendingCoachTaskId(null);
              setConfig(profileRunConfig(project.agent_profiles.paper_coach));
              skills.reset();
              setSubmitError(null);
            }}
          >
            <MessageCirclePlus size={14} />
          </button>
        </div>

        <div
          className="agent-provider-label"
          aria-busy={readiness === undefined}
          aria-label={`Writing coach provider: ${readiness?.label || config.provider}`}
        >
          {readiness?.label || config.provider}
          {readiness === undefined && (
            <LoaderCircle className="spin" size={12} aria-label="Checking provider" />
          )}
        </div>
        {reviewStale && (
          <div className="stale-review">
            <TriangleAlert size={14} /> Project understanding changed since this session reviewed
            the draft.
          </div>
        )}
        {readiness && !readiness.authenticated && (
          <div className="provider-unavailable">
            <WifiOff size={14} /> {readiness?.reason || `${config.provider} is unavailable.`}
          </div>
        )}

        <div className="coach-transcript" aria-live="polite">
          {transcript.map((line) => (
            <div
              className={`chat-line ${line.role === "agent" ? "coach" : line.role}`}
              key={line.lineId}
            >
              {line.role === "human" ? `You: ${line.text}` : line.text}
            </div>
          ))}
          {submitError && <div className="chat-line error">{submitError}</div>}
          {coachActive && (
            <div className="thinking">
              <i />
              <i />
              <i /> {activeCoachTask?.status_message || "Writing coach is running"}
            </div>
          )}
        </div>
        <div className="coach-composer">
          <SkillPicker {...skills.props} />
          <textarea
            ref={coachTextarea}
            aria-label="Message"
            value={message}
            onChange={(event) => updateMessage(event.target.value)}
            onKeyDown={(event) => {
              if (skills.handleKeyDown(event)) return;
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <button
            className="icon-button primary"
            aria-label={
              activeSession ? "Resume session in background" : "Start coach task in background"
            }
            disabled={
              !message.trim() ||
              Boolean(activeCoachTask) ||
              coachSubmitting ||
              (readiness !== undefined && !readiness.authenticated)
            }
            onClick={() => void send()}
          >
            <Send size={16} />
          </button>
        </div>
      </aside>
    </section>
  );
}
