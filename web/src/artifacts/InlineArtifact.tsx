import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { FileX, Maximize2, MessageSquarePlus, RefreshCw, TriangleAlert } from "lucide-react";
import { useResource, useRefreshRuntime } from "../core/useResource";
import { resourceKeyId } from "../core/resourceStore";
import { artifactStateProfile } from "./artifactResources";
import { errorMessage } from "../core/errors";
import type { AgentArtifactDescriptor } from "../core/types";
import { INLINE_ARTIFACT_HEIGHT, inlineViewerUrl, type InlineSelection } from "./inlineArtifacts";
import { useInlineShell } from "./useInlineShell";

export interface InlineArtifactSelectionEvent {
  selection: InlineSelection | null;
  initialText?: string;
  description: string;
  /** Where the comment window anchors, in the chat window's coordinates. */
  anchor: { left: number; right: number; top: number };
  /** The viewer says the origin cannot resume, so the edit needs a new session. */
  freshSession: boolean;
  /** The version the reader selected on, so a newer one refuses the edit. */
  version: string | null;
  clear: () => void;
}

interface InlineArtifactProps {
  projectId: string;
  /** Null while the turn that owns it loads; the same element then shows it. */
  artifact: AgentArtifactDescriptor | null;
  title: string;
  /** Fetches the owning turn once, when an embed mounts without its artifact. */
  onLoadTask?: () => void;
  /** Changes when a turn in this chat settles, so a published edit is picked up. */
  refreshToken: string;
  /** Whether this reader may stage comments; false in a read-only transcript. */
  canComment: boolean;
  keeping: boolean;
  /** Why the last Keep or Download of this artifact failed, shown in its caption. */
  actionError?: string;
  onExpand: () => void;
  onKeep: (() => void) | null;
  download: ReactNode;
  onSelection: (event: InlineArtifactSelectionEvent) => void;
  /** A selection is gone: cancelled in the frame (Escape, a click, a new drag) or comment
   * mode ended. `clear` is the one the selection was reported with. */
  onSelectionCancel: (clear: () => void) => void;
  /** A new version replaced the frame, so selections drawn on the old one are stale. */
  onVersionChange: () => void;
}

/** One artifact shown in place inside a reply: the viewer shell in a fixed-height frame.
 *
 * The frame has its final height before it loads, so loading, a refresh, or a new
 * version never moves the reply around it; taller content scrolls inside. */
export function InlineArtifact({
  projectId,
  artifact,
  title,
  onLoadTask,
  canComment,
  keeping,
  actionError,
  onExpand,
  onKeep,
  download,
  onSelection,
  onSelectionCancel,
  onVersionChange,
}: InlineArtifactProps) {
  const host = useRef<HTMLSpanElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const [near, setNear] = useState(false);
  const { store } = useRefreshRuntime();
  const owner = useId();
  const resource = useResource(
    `${projectId}:inline:${owner}`,
    near && artifact ? artifactStateProfile(projectId, artifact.artifact_id) : null,
  );
  const state = resource.value;
  const error = resource.error ? errorMessage(resource.error) : "";
  const [pageError, setPageError] = useState<{ message: string; count: number } | null>(null);
  const [commentMode, setCommentMode] = useState(false);
  const commentModeRef = useRef(commentMode);
  commentModeRef.current = commentMode;
  const onVersionChangeRef = useRef(onVersionChange);
  onVersionChangeRef.current = onVersionChange;
  const freshSessionRef = useRef(false);
  freshSessionRef.current = Boolean(state?.fresh_session_required);

  const loadTask = useRef(onLoadTask);
  loadTask.current = onLoadTask;
  useEffect(() => {
    loadTask.current?.();
  }, []);

  // A long chat mounts only the frames a reader is about to reach.
  useEffect(() => {
    const element = host.current;
    if (!element || near) return;
    if (typeof IntersectionObserver === "undefined") {
      setNear(true);
      return;
    }
    // The margin reaches past the chat's own scroll box only when that box is the root.
    let root = element.parentElement;
    while (root && !/auto|scroll/.test(getComputedStyle(root).overflowY)) root = root.parentElement;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setNear(true);
      },
      { root, rootMargin: "800px 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [near]);

  // A new version replaces the frame, so a comment staged on the old one has no
  // region left to point at.
  const shownVersion = useRef<string | null>(null);
  const currentVersion = state?.current_version ?? null;
  useEffect(() => {
    if (currentVersion === null) return;
    if (shownVersion.current !== null && shownVersion.current !== currentVersion)
      onVersionChangeRef.current();
    shownVersion.current = currentVersion;
    setPageError(null);
  }, [currentVersion]);

  const shell = useInlineShell(frame, commentMode, {
    onPageError: setPageError,
    onCancel: onSelectionCancel,
    onSelection: (event) =>
      onSelection({
        ...event,
        freshSession: freshSessionRef.current,
        version: shownVersion.current,
      }),
  });

  const selectable =
    canComment &&
    Boolean(state?.can_comment) &&
    (artifact?.view === "html" || artifact?.view === "image");
  // A refresh can withdraw commenting, such as another edit reserving the
  // artifact, and would leave comment mode on with no Done to end it.
  useEffect(() => {
    if (selectable || !commentModeRef.current) return;
    setCommentMode(false);
    shell.cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runs when commenting is withdrawn
  }, [selectable]);
  if (!artifact)
    return (
      <span ref={host} className="chat-inline-artifact">
        <span
          className="chat-inline-artifact-placeholder"
          style={{ height: INLINE_ARTIFACT_HEIGHT }}
          role="status"
          aria-label={`Loading ${title}`}
        />
        <span className="chat-inline-artifact-bar">
          <span className="chat-inline-artifact-name">{title}</span>
        </span>
      </span>
    );
  const source = state?.viewer_url ? inlineViewerUrl(state.viewer_url) : null;
  const pageErrorText = pageError ? `This page hit an error: ${pageError.message}` : "";
  return (
    <span
      ref={host}
      className={`chat-inline-artifact ${artifact.view}${commentMode ? " commenting" : ""}${error || pageError ? " failed" : ""}`}
      data-artifact-id={artifact.artifact_id}
    >
      {/* One box from mount on: the frame goes inside it rather than replacing it,
          since WebKit moved the scroll when a box left the page and another took its place. */}
      <span
        className={source ? "chat-inline-artifact-box" : "chat-inline-artifact-placeholder"}
        style={{ height: INLINE_ARTIFACT_HEIGHT }}
        role={source ? undefined : error ? "alert" : "status"}
      >
        {source ? (
          <iframe
            ref={frame}
            key={`${state?.current_version}:${source}`}
            className="chat-inline-artifact-frame"
            src={source}
            title={title}
            onLoad={shell.onLoad}
          />
        ) : (
          error && (
            <button
              aria-label="Retry artifact"
              title={error}
              onClick={() =>
                store.invalidate(
                  resourceKeyId(artifactStateProfile(projectId, artifact.artifact_id).key),
                )
              }
            >
              <RefreshCw size={14} />
            </button>
          )
        )}
      </span>
      <span className="chat-inline-artifact-bar">
        <span className="chat-inline-artifact-name" title={artifact.name}>
          {title}
          {state && state.version_number > 1 && <em>v{state.version_number}</em>}
          {state?.live && (
            <em className={`chat-inline-artifact-live ${state.live}`}>
              {state.live === "live" ? "Live" : "Finished"}
            </em>
          )}
          {state?.editing_operation_id && <em>Editing…</em>}
        </span>
        {selectable && (
          <button
            type="button"
            className="chat-inline-artifact-comment"
            aria-pressed={commentMode}
            title={
              commentMode
                ? "Stop commenting and use the artifact"
                : "Select a part of this artifact to comment on it"
            }
            aria-label={commentMode ? "Done commenting" : "Comment on a part"}
            onClick={() => {
              // Leaving comment mode clears the selection without a message from the frame.
              if (commentMode) shell.cancel();
              setCommentMode(!commentMode);
            }}
          >
            <MessageSquarePlus size={12} />
            <span className="chat-inline-artifact-label">{commentMode ? "Done" : "Comment"}</span>
          </button>
        )}
        <button type="button" onClick={onExpand} title="Open in the viewer" aria-label="Expand">
          <Maximize2 size={12} />
          <span className="chat-inline-artifact-label">Expand</span>
        </button>
        {onKeep && state?.can_keep !== false && (
          <button type="button" data-artifact-action="keep" disabled={keeping} onClick={onKeep}>
            Save to Artifacts
          </button>
        )}
        {download}
        {pageError &&
          (selectable ? (
            <button
              type="button"
              className="chat-inline-artifact-page-error"
              data-artifact-action="fix"
              data-error-count={pageError.count}
              title={pageErrorText}
              aria-label="Ask to fix"
              onClick={(event) => {
                const rect = event.currentTarget.getBoundingClientRect();
                onSelection({
                  selection: null,
                  description: artifact.name,
                  initialText: pageErrorText,
                  anchor: { left: rect.left, right: rect.right, top: rect.top },
                  freshSession: freshSessionRef.current,
                  version: shownVersion.current,
                  clear: () => {},
                });
              }}
            >
              <TriangleAlert size={12} />
            </button>
          ) : (
            <span
              className="chat-inline-artifact-page-error"
              role="status"
              data-error-count={pageError.count}
              title={pageErrorText}
              aria-label={pageErrorText}
            >
              <TriangleAlert size={12} />
            </span>
          ))}
        {actionError && (
          <strong className="chat-inline-artifact-error" role="alert">
            {actionError}
          </strong>
        )}
        {commentMode && (
          <span className="chat-inline-artifact-hint" role="status">
            Drag across a part, or select its text, to comment on it.
          </span>
        )}
      </span>
    </span>
  );
}

/** `attached` says where a PDF or download-only embed's card is: this reply or an earlier one. */
export function InlineArtifactMissing({
  name,
  attached,
}: {
  name: string;
  attached?: "below" | "earlier";
}) {
  return (
    <span className="chat-inline-artifact missing" role="status">
      <FileX size={12} aria-hidden="true" />{" "}
      {attached === "below"
        ? `${name} is attached below.`
        : attached === "earlier"
          ? `${name} is attached to an earlier reply.`
          : `${name} is not available.`}
    </span>
  );
}
