import { useEffect, useRef, useState, type ReactNode } from "react";
import { FileX, Maximize2, MessageSquarePlus } from "lucide-react";
import { api } from "../core/api";
import { errorMessage } from "../core/errors";
import type { AgentArtifactDescriptor, ArtifactViewerState } from "../core/types";
import {
  INLINE_ARTIFACT_INITIAL_HEIGHT,
  inlineViewerUrl,
  readInlineShellMessage,
  type InlineSelection,
} from "./inlineArtifacts";

export interface InlineArtifactSelectionEvent {
  selection: InlineSelection;
  description: string;
  /** Where the comment window anchors, in the chat window's coordinates. */
  anchor: { left: number; right: number; top: number };
  /** The viewer says the origin cannot resume, so the edit needs a new session. */
  freshSession: boolean;
  clear: () => void;
}

interface InlineArtifactProps {
  projectId: string;
  artifact: AgentArtifactDescriptor;
  title: string;
  /** Changes when a turn in this chat settles, so a published edit is picked up. */
  refreshToken: string;
  /** Whether this reader may stage comments; false in a read-only transcript. */
  canComment: boolean;
  keeping: boolean;
  onExpand: () => void;
  onKeep: (() => void) | null;
  download: ReactNode;
  onSelection: (event: InlineArtifactSelectionEvent) => void;
}

/** One artifact shown in place inside a reply: the viewer shell, sized by its content. */
export function InlineArtifact({
  projectId,
  artifact,
  title,
  refreshToken,
  canComment,
  keeping,
  onExpand,
  onKeep,
  download,
  onSelection,
}: InlineArtifactProps) {
  const host = useRef<HTMLSpanElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const [near, setNear] = useState(false);
  const [state, setState] = useState<ArtifactViewerState | null>(null);
  const [error, setError] = useState("");
  const [height, setHeight] = useState(INLINE_ARTIFACT_INITIAL_HEIGHT);
  const [commentMode, setCommentMode] = useState(false);
  const commentModeRef = useRef(commentMode);
  commentModeRef.current = commentMode;
  const onSelectionRef = useRef(onSelection);
  onSelectionRef.current = onSelection;
  const freshSessionRef = useRef(false);
  freshSessionRef.current = Boolean(state?.fresh_session_required);

  // A long chat mounts only the frames a reader is about to reach.
  useEffect(() => {
    const element = host.current;
    if (!element || near) return;
    if (typeof IntersectionObserver === "undefined") {
      setNear(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setNear(true);
      },
      { rootMargin: "800px 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [near]);

  useEffect(() => {
    if (!near) return;
    const controller = new AbortController();
    api<ArtifactViewerState>(
      `/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}/state`,
      { signal: controller.signal },
    )
      .then((next) => {
        setState(next);
        setError("");
      })
      .catch((failure) => {
        if (!controller.signal.aborted) setError(errorMessage(failure));
      });
    return () => controller.abort();
  }, [near, projectId, artifact.artifact_id, refreshToken]);

  useEffect(() => {
    const message = (event: MessageEvent) => {
      const value = readInlineShellMessage(
        event,
        frame.current?.contentWindow,
        window.location.origin,
      );
      if (!value) return;
      if (value.kind === "size") {
        setHeight(value.height);
        return;
      }
      if (!value.selection || !commentModeRef.current) return;
      const rect = frame.current?.getBoundingClientRect();
      if (!rect) return;
      const box = value.selection.kind === "box" ? value.selection.rect : null;
      onSelectionRef.current({
        selection: value.selection,
        description: value.description,
        anchor: box
          ? {
              left: rect.left + box.x * rect.width,
              right: rect.left + (box.x + box.width) * rect.width,
              top: rect.top + box.y * rect.height,
            }
          : { left: rect.left, right: rect.right, top: rect.top + Math.min(rect.height, 48) },
        freshSession: freshSessionRef.current,
        clear: () =>
          frame.current?.contentWindow?.postMessage(
            { type: "rcp-inline-selection-clear", version: 1 },
            window.location.origin,
          ),
      });
    };
    window.addEventListener("message", message);
    return () => window.removeEventListener("message", message);
  }, []);

  const postCommentMode = (enabled: boolean) =>
    frame.current?.contentWindow?.postMessage(
      { type: "rcp-inline-comment-mode", version: 1, enabled },
      window.location.origin,
    );
  useEffect(() => {
    postCommentMode(commentMode);
  }, [commentMode]);

  const selectable =
    canComment &&
    Boolean(state?.can_comment) &&
    (artifact.view === "html" || artifact.view === "image");
  const source = state?.viewer_url ? inlineViewerUrl(state.viewer_url) : null;
  return (
    <span
      ref={host}
      className={`chat-inline-artifact ${artifact.view}${commentMode ? " commenting" : ""}`}
      data-artifact-id={artifact.artifact_id}
    >
      {source ? (
        <iframe
          ref={frame}
          key={`${state?.current_version}:${source}`}
          className="chat-inline-artifact-frame"
          src={source}
          title={title}
          style={{ height }}
          onLoad={() => postCommentMode(commentModeRef.current)}
        />
      ) : (
        <span
          className="chat-inline-artifact-placeholder"
          style={{ height: error ? undefined : INLINE_ARTIFACT_INITIAL_HEIGHT }}
          role={error ? "alert" : "status"}
        >
          {error ? `${title}: ${error}` : null}
        </span>
      )}
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
            aria-pressed={commentMode}
            title={
              commentMode
                ? "Stop commenting and use the artifact"
                : "Select a part of this artifact to comment on it"
            }
            aria-label={commentMode ? "Done commenting" : "Comment on a part"}
            onClick={() => setCommentMode((current) => !current)}
          >
            <MessageSquarePlus size={12} />
            <span className="chat-inline-artifact-label">{commentMode ? "Done" : "Comment"}</span>
          </button>
        )}
        <button type="button" onClick={onExpand} title="Open in the viewer" aria-label="Expand">
          <Maximize2 size={12} />
          <span className="chat-inline-artifact-label">Expand</span>
        </button>
        {onKeep && (
          <button type="button" data-artifact-action="keep" disabled={keeping} onClick={onKeep}>
            Keep
          </button>
        )}
        {download}
        {commentMode && (
          <span className="chat-inline-artifact-hint" role="status">
            Drag across a part, or select its text, to comment on it.
          </span>
        )}
      </span>
    </span>
  );
}

export function InlineArtifactMissing({ name, attached }: { name: string; attached?: boolean }) {
  return (
    <span className="chat-inline-artifact missing" role="status">
      <FileX size={12} aria-hidden="true" />{" "}
      {attached ? `${name} is attached below.` : `${name} is not available.`}
    </span>
  );
}
