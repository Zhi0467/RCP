import { useEffect, useRef, useState } from "react";
import { Download, ExternalLink, File } from "lucide-react";
import { api } from "../core/api";
import { isDesktopRuntime } from "../core/desktopRuntime";
import type { AgentArtifactDescriptor, AgentTask, ProjectSnapshot } from "../core/types";
import { taskArtifacts, type TaskTranscriptLine } from "../agents/agentTasks";
import {
  InlineArtifact,
  InlineArtifactMissing,
  type InlineArtifactSelectionEvent,
} from "../artifacts/InlineArtifact";
import { inlineArtifactIds, isInlineViewable, turnArtifactFor } from "../artifacts/inlineArtifacts";
import {
  openArtifact as openArtifactPanel,
  openRepositoryFile as openRepositoryFilePanel,
} from "../artifacts/artifactViewerModel";
import {
  artifactReferenceId,
  isReferenceHrefCandidate,
  resolveRepositoryFileHref,
  turnArtifactLocation,
} from "../core/repositoryFileLinks";
import { formatBytes } from "../chat/composerAttachments";
import type { AnswerComments } from "../chat/answerComments";
import type { AgentWindowEndpoints } from "./agentWindowEndpoints";
import { ReferenceEmbed, resolveReference } from "./ReferenceEmbed";

const INLINE_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024;
const NO_REPOSITORIES: ProjectSnapshot["repositories"] = [];
export function useAgentWindowArtifacts({
  endpoints,
  chatId,
  relatedTasks,
  transcript,
  comments,
  onRefreshTask,
  setSubmitError,
  inlineArtifactRefreshToken = "",
  readOnly = false,
  allowArtifactComments = false,
  repositories = NO_REPOSITORIES,
}: {
  endpoints: AgentWindowEndpoints;
  chatId: string;
  relatedTasks: AgentTask[];
  transcript: TaskTranscriptLine[];
  comments: AnswerComments;
  onRefreshTask: (taskId: string) => Promise<AgentTask>;
  setSubmitError: (error: string | null) => void;
  inlineArtifactRefreshToken?: string;
  readOnly?: boolean;
  allowArtifactComments?: boolean;
  repositories?: ProjectSnapshot["repositories"];
}) {
  const { annotations } = comments;
  const ownerRef = useRef({ key: endpoints.scopeKey });
  if (ownerRef.current.key !== endpoints.scopeKey) ownerRef.current = { key: endpoints.scopeKey };
  const owner = ownerRef.current;
  const isCurrent = () => ownerRef.current === owner;
  useEffect(() => {
    ownerRef.current = owner;
    return () => {
      if (ownerRef.current === owner) ownerRef.current = { key: "" };
    };
  }, [owner]);

  const desktop = isDesktopRuntime() && endpoints.downloadArtifact !== null;
  // Embedded turns that aged out of the recent task list, fetched once on demand.
  // Kept here because a later bounded task-list refresh drops such tasks again.
  const [agedInlineArtifacts, setAgedInlineArtifacts] = useState<
    ReadonlyMap<string, AgentArtifactDescriptor[] | "loading">
  >(() => new Map());
  const [failedArtifactPreviews, setFailedArtifactPreviews] = useState<Set<string>>(
    () => new Set(),
  );
  const [artifactShellErrors, setArtifactShellErrors] = useState<Map<string, string>>(
    () => new Map(),
  );
  const [keepingArtifacts, setKeepingArtifacts] = useState<Set<string>>(() => new Set());
  const [repositoryFileErrors, setRepositoryFileErrors] = useState<Map<string, string>>(
    () => new Map(),
  );

  useEffect(() => {
    setAgedInlineArtifacts(new Map());
    setRepositoryFileErrors(new Map());
    setArtifactShellErrors(new Map());
    setFailedArtifactPreviews(new Set());
    setKeepingArtifacts(new Set());
  }, [endpoints.scopeKey]);
  // A part of an artifact shown inside a reply, selected in its comment mode, is
  // staged like answer text; the turn carries it as that artifact's selection.
  const openInlineArtifactComment = (
    taskId: string,
    artifact: AgentArtifactDescriptor,
    event: InlineArtifactSelectionEvent,
  ) => {
    if (
      annotations.some(
        (annotation) =>
          annotation.artifact && annotation.artifact.context.artifact_id !== artifact.artifact_id,
      )
    ) {
      setSubmitError(
        "One message carries comments on one artifact. Send or remove the staged ones first.",
      );
      event.clear();
      return;
    }
    // A prefill joins an unsent draft on this artifact below its text, keeping
    // the draft's own selection, rather than replacing it.
    const open = comments.composer?.step === "comment" ? comments.composer : null;
    const draft = open ? comments.comment.trim() : "";
    const prefill = event.initialText ?? "";
    if (draft && prefill && open?.artifact?.context.artifact_id === artifact.artifact_id) {
      if (!draft.includes(prefill)) comments.setComment(`${draft}\n\n${prefill}`);
      comments.focusComment();
      return;
    }
    comments.openExternal(
      {
        selectedText: event.description || artifact.name,
        anchor: event.anchor,
        artifact: {
          context: {
            source: "task",
            operation_id: taskId,
            artifact_id: artifact.artifact_id,
            ...(event.freshSession ? { fresh_session: true } : {}),
            ...(event.version ? { base_version: event.version } : {}),
          },
          name: artifact.name,
          selection: event.selection ? { ...event.selection, comment: "" } : null,
        },
      },
      { clear: event.clear, ...(prefill ? { draft: prefill } : {}) },
    );
  };

  // A new version moves the parts a selection named, so comments on the old one,
  // open or staged, would be sent against bytes the reader never saw.
  const dropStaleArtifactComments = (artifact: AgentArtifactDescriptor) => {
    const open =
      comments.composer?.step === "comment" ? comments.composer.artifact?.context : undefined;
    if (open?.artifact_id === artifact.artifact_id) comments.closeComment({ keepText: true });
    const stale = annotations.filter(
      (annotation) => annotation.artifact?.context.artifact_id === artifact.artifact_id,
    );
    if (!stale.length) return;
    comments.discard(stale);
    setSubmitError(
      `${artifact.name} changed to a new version, so its staged comments were removed. Select the parts again.`,
    );
  };

  const markArtifactPreviewFailed = (taskId: string, artifactId: string) => {
    setFailedArtifactPreviews((current) => {
      const next = new Set(current);
      next.add(`${taskId}:${artifactId}`);
      return next;
    });
  };

  const openArtifact = async (taskId: string, artifact: AgentArtifactDescriptor) => {
    const systemPdf = desktop && artifact.view === "pdf" && artifact.can_download;
    if (!artifact.can_open && !systemPdf) return;
    const key = `${taskId}:${artifact.artifact_id}`;
    setArtifactShellErrors((current) => withoutMapKey(current, key));
    try {
      await endpoints.openArtifact(taskId, artifact);
    } catch (error) {
      if (!isCurrent()) return;
      setArtifactShellErrors((current) =>
        withMapValue(
          current,
          key,
          `Open failed: ${error instanceof Error ? error.message : String(error)}`,
        ),
      );
    }
  };

  const downloadArtifact = async (taskId: string, artifact: AgentArtifactDescriptor) => {
    if (!artifact.can_download) return;
    const key = `${taskId}:${artifact.artifact_id}`;
    setArtifactShellErrors((current) => withoutMapKey(current, key));
    try {
      await endpoints.downloadArtifact?.(taskId, artifact);
    } catch (error) {
      if (!isCurrent()) return;
      setArtifactShellErrors((current) =>
        withMapValue(
          current,
          key,
          `Download failed: ${error instanceof Error ? error.message : String(error)}`,
        ),
      );
    }
  };

  const keepArtifact = async (taskId: string, artifact: AgentArtifactDescriptor) => {
    const key = `${taskId}:${artifact.artifact_id}`;
    if (!endpoints.canKeep || !artifact.can_keep || keepingArtifacts.has(key)) return;
    setKeepingArtifacts((current) => new Set(current).add(key));
    setArtifactShellErrors((current) => withoutMapKey(current, key));
    try {
      await api(endpoints.artifactUrl(taskId, artifact.artifact_id, "keep"), {
        method: "POST",
        body: JSON.stringify({}),
      });
      const task = await onRefreshTask(taskId);
      if (!isCurrent()) return;
      // An aged-out turn's embed reads its cached descriptors, which still offer Keep.
      setAgedInlineArtifacts((current) =>
        Array.isArray(current.get(taskId))
          ? new Map(current).set(taskId, taskArtifacts(task))
          : current,
      );
    } catch (error) {
      if (!isCurrent()) return;
      setArtifactShellErrors((current) =>
        withMapValue(current, key, error instanceof Error ? error.message : String(error)),
      );
    } finally {
      if (isCurrent())
        setKeepingArtifacts((current) => {
          const next = new Set(current);
          next.delete(key);
          return next;
        });
    }
  };

  // A repository file or saved artifact the reply references where it lives.
  const resolveOriginal = (href: string) => resolveReference(href, endpoints, repositories);
  const openOriginal = async (messageId: string, href: string) => {
    const found = await resolveOriginal(href);
    if (!isCurrent()) return;
    if (found.kind === "unresolved") {
      setRepositoryFileErrors((current) => withMapValue(current, messageId, found.reason));
      return;
    }
    setRepositoryFileErrors((current) => withoutMapKey(current, messageId));
    if (found.kind === "artifact")
      openArtifactPanel({ projectId: found.projectId, artifactId: found.artifactId });
    else
      openRepositoryFilePanel({
        projectId: found.projectId,
        path: found.target.path,
        line: found.target.line ?? undefined,
      });
  };

  const openRepositoryFile = async (messageId: string, taskId: string, href: string) => {
    if (artifactReferenceId(href)) return openOriginal(messageId, href);
    const resolution = resolveRepositoryFileHref(href, repositories);
    if (resolution.kind === "error") {
      // An answer may cite a file the turn itself wrote. That path is outside every
      // repository, so the artifact the task already registered owns the preview.
      // Only when no root claims the path. Several matching roots stay a visible
      // error, because the reader must not be handed a guess about which one won.
      const location = resolution.reason === "no-match" ? turnArtifactLocation(href) : null;
      if (location) {
        // An older answer's task, or the turn it cites, has aged out of the recent
        // list, so fetch the exact task rather than refusing a citation the
        // transcript still displays.
        const loaded = new Map<string, AgentArtifactDescriptor[]>();
        const load = async (id: string) => {
          loaded.set(id, knownTaskArtifacts(id) ?? (await loadTaskArtifacts(id)));
        };
        await load(taskId);
        let found = turnArtifactFor(href, taskId, (id) => loaded.get(id));
        if (found?.loading) {
          await load(found.taskId);
          found = turnArtifactFor(href, taskId, (id) => loaded.get(id));
        }
        if (!isCurrent()) return;
        const artifact = found?.artifact;
        if (found && artifact) {
          const owner = found.taskId;
          setRepositoryFileErrors((current) => withoutMapKey(current, messageId));
          if (artifact.can_open || (desktop && artifact.view === "pdf" && artifact.can_download)) {
            await openArtifact(owner, artifact);
          } else {
            if (!relatedTasks.some((candidate) => candidate.operation_id === owner))
              await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
            if (!isCurrent()) return;
            const card = document.getElementById(`artifact-${owner}-${artifact.artifact_id}`);
            card?.scrollIntoView({ block: "nearest" });
            card?.focus();
          }
          return;
        }
      }
      // A Space window's repositories are its member's projects', which its resolver reads.
      if (endpoints.referenceProject) return openOriginal(messageId, href);
      // A path no repository or turn folder owns, such as an API route, is an inert link.
      if (resolution.reason !== "no-match")
        setRepositoryFileErrors((current) => withMapValue(current, messageId, resolution.message));
      return;
    }

    setRepositoryFileErrors((current) => withoutMapKey(current, messageId));
    openRepositoryFilePanel({
      projectId: endpoints.projectId!,
      path: resolution.target.path,
      line: resolution.target.line ?? undefined,
    });
  };

  const artifactDownloadControl = (taskId: string, artifact: AgentArtifactDescriptor) => {
    if (!artifact.can_download) return null;
    const label = <span className="chat-inline-artifact-label">Download</span>;
    return desktop ? (
      <button
        type="button"
        aria-label="Download"
        onClick={() => void downloadArtifact(taskId, artifact)}
      >
        <Download size={12} />
        {label}
      </button>
    ) : (
      <a
        href={endpoints.artifactUrl(taskId, artifact.artifact_id, "download")}
        download={artifact.name}
        aria-label="Download"
      >
        <Download size={12} />
        {label}
      </a>
    );
  };

  // A turn's own descriptors, or the ones an embed fetched after the turn aged out,
  // so a PDF the reply says is attached below keeps its card.
  const lineArtifacts = (line: TaskTranscriptLine) => {
    if (line.artifacts !== undefined || line.role !== "agent") return line.artifacts;
    const aged = agedInlineArtifacts.get(line.taskId);
    return Array.isArray(aged) ? aged : undefined;
  };

  // A task's descriptors from the recent list or the on-demand cache; undefined until loaded.
  const knownTaskArtifacts = (taskId: string) => {
    const task = relatedTasks.find((candidate) => candidate.operation_id === taskId);
    if (task) return taskArtifacts(task);
    const aged = agedInlineArtifacts.get(taskId);
    return Array.isArray(aged) ? aged : undefined;
  };

  // The chat this transcript shows; an artifact is only ever shown in its origin chat.
  const transcriptChatId = chatId ?? relatedTasks.at(-1)?.request.chat_id ?? null;

  // A turn outside the recent list, fetched once and kept, since a later bounded
  // task-list refresh drops it again. A cited turn of another chat contributes nothing.
  const loadTaskArtifacts = (taskId: string): Promise<AgentArtifactDescriptor[]> => {
    setAgedInlineArtifacts((current) =>
      current.has(taskId) ? current : new Map(current).set(taskId, "loading"),
    );
    return onRefreshTask(taskId)
      .then((task) =>
        transcript.some((line) => line.taskId === taskId) ||
        task.request.chat_id === transcriptChatId
          ? taskArtifacts(task)
          : [],
      )
      .catch((): AgentArtifactDescriptor[] => [])
      .then((loaded) => {
        if (isCurrent()) setAgedInlineArtifacts((current) => new Map(current).set(taskId, loaded));
        return loaded;
      });
  };

  const renderInlineArtifact = (
    taskId: string,
    artifacts: AgentArtifactDescriptor[] | undefined,
    src: string,
    alt: string,
  ) => {
    const found = turnArtifactFor(src, taskId, (id) =>
      id === taskId && artifacts !== undefined ? artifacts : knownTaskArtifacts(id),
    );
    // Any other repository file or saved artifact shows from where it lives.
    if (!found)
      return isReferenceHrefCandidate(src) ? (
        <ReferenceEmbed
          key={src}
          href={src}
          title={alt}
          resolve={resolveOriginal}
          refreshToken={inlineArtifactRefreshToken}
        />
      ) : null;
    const { artifact } = found;
    // An older reply's task, or the earlier turn it cites, has aged out of the recent
    // list, so fetch the exact task before deciding the artifact is missing.
    // Loading moves from the citing turn to the cited one; each loads on mount.
    if (found.loading)
      return renderArtifactPlayer(found.taskId, found.name, null, found.name, () => {
        if (!agedInlineArtifacts.has(found.taskId)) void loadTaskArtifacts(found.taskId);
      });
    // A PDF or download-only file keeps its card below the reply.
    if (artifact && !isInlineViewable(artifact))
      return (
        <InlineArtifactMissing
          name={found.name}
          attached={found.taskId === taskId ? "below" : "earlier"}
        />
      );
    if (!artifact || !artifact.available || !artifact.can_open)
      return <InlineArtifactMissing name={found.name} />;
    return renderArtifactPlayer(found.taskId, found.name, artifact, alt.trim() || artifact.name);
  };

  // Keyed by the cited name, so a loading embed becomes its artifact in the same
  // element: replacing the element let WebKit move the reader's scroll.
  const renderArtifactPlayer = (
    taskId: string,
    slot: string,
    artifact: AgentArtifactDescriptor | null,
    title: string,
    onLoadTask?: () => void,
  ) => {
    const key = `${taskId}:${artifact?.artifact_id}`;
    return (
      <InlineArtifact
        key={`${taskId}:${slot}`}
        projectId={endpoints.projectId}
        stateProfile={
          artifact && endpoints.artifactState ? endpoints.artifactState(artifact.artifact_id) : null
        }
        viewerUrl={
          artifact && !endpoints.artifactState
            ? endpoints.artifactUrl(taskId, artifact.artifact_id, "viewer")
            : undefined
        }
        artifact={artifact}
        title={title}
        refreshToken={inlineArtifactRefreshToken}
        canComment={endpoints.canCommentArtifacts && (!readOnly || allowArtifactComments)}
        keeping={keepingArtifacts.has(key)}
        actionError={artifactShellErrors.get(key)}
        onLoadTask={onLoadTask}
        onExpand={() => artifact && void openArtifact(taskId, artifact)}
        onKeep={
          endpoints.canKeep && artifact?.can_keep ? () => void keepArtifact(taskId, artifact) : null
        }
        download={artifact && artifactDownloadControl(taskId, artifact)}
        onSelection={(event) => artifact && openInlineArtifactComment(taskId, artifact, event)}
        onSelectionCancel={comments.cancelExternal}
        onVersionChange={() => artifact && dropStaleArtifactComments(artifact)}
      />
    );
  };

  const renderArtifacts = (line: TaskTranscriptLine) => (
    <>
      {" "}
      {lineArtifacts(line)?.map((artifact) => {
        // A reply that embeds an artifact shows it in place, with its actions.
        if (
          line.role === "agent" &&
          line.text &&
          isInlineViewable(artifact) &&
          artifact.available &&
          artifact.can_open &&
          inlineArtifactIds(line.text, line.taskId, lineArtifacts(line) ?? []).has(
            artifact.artifact_id,
          )
        )
          return null;
        if (artifact.view === "video" && artifact.available && artifact.can_open)
          return renderArtifactPlayer(line.taskId, artifact.name, artifact, artifact.name);
        const taskUpdatedAt =
          relatedTasks.find((task) => task.operation_id === line.taskId)?.updated_at ?? "";
        const previewFailed = failedArtifactPreviews.has(`${line.taskId}:${artifact.artifact_id}`);
        const unavailable = !artifact.available;
        const unavailableReason =
          (!artifact.available && artifact.unavailable_reason) ||
          (previewFailed ? "Preview unavailable" : null);
        const shellError = artifactShellErrors.get(`${line.taskId}:${artifact.artifact_id}`);
        return (
          <div
            className={`chat-artifact${unavailable ? " unavailable" : ""}`}
            key={artifact.artifact_id}
            id={`artifact-${line.taskId}-${artifact.artifact_id}`}
            tabIndex={-1}
          >
            {artifact.view === "image" &&
              artifact.size_bytes != null &&
              artifact.size_bytes <= INLINE_ARTIFACT_MAX_BYTES &&
              artifact.can_open &&
              !unavailable &&
              !previewFailed && (
                <button
                  className="chat-artifact-inline"
                  type="button"
                  aria-label={`Open ${artifact.name}`}
                  onClick={() => void openArtifact(line.taskId, artifact)}
                >
                  <img
                    src={`${endpoints.artifactUrl(line.taskId, artifact.artifact_id, "content")}?task_updated_at=${encodeURIComponent(taskUpdatedAt)}`}
                    alt={artifact.name}
                    onError={() => markArtifactPreviewFailed(line.taskId, artifact.artifact_id)}
                  />
                </button>
              )}
            <File size={14} />
            <span>
              {artifact.name}
              {artifact.size_bytes != null && ` · ${formatBytes(artifact.size_bytes)}`}
              {(artifact.kept_at || artifact.kept_filename) && <em>Saved</em>}
            </span>
            {unavailableReason && <strong>{unavailableReason}</strong>}
            <div className="chat-artifact-actions">
              {(artifact.can_open ||
                (desktop && artifact.view === "pdf" && artifact.can_download)) && (
                <button type="button" onClick={() => void openArtifact(line.taskId, artifact)}>
                  <ExternalLink size={12} /> Open
                </button>
              )}
              {artifact.can_download &&
                (desktop ? (
                  <button
                    type="button"
                    onClick={() => void downloadArtifact(line.taskId, artifact)}
                  >
                    <Download size={12} /> Download
                  </button>
                ) : (
                  <a
                    href={endpoints.artifactUrl(line.taskId, artifact.artifact_id, "download")}
                    download={artifact.name}
                  >
                    <Download size={12} /> Download
                  </a>
                ))}
              {endpoints.canKeep && artifact.can_keep && (
                <button
                  type="button"
                  data-artifact-action="keep"
                  disabled={keepingArtifacts.has(`${line.taskId}:${artifact.artifact_id}`)}
                  onClick={() => void keepArtifact(line.taskId, artifact)}
                >
                  Save to Artifacts
                </button>
              )}
            </div>
            {shellError && (
              <strong className="chat-artifact-shell-error" role="alert">
                {shellError}
              </strong>
            )}
          </div>
        );
      })}{" "}
    </>
  );
  return { renderInlineArtifact, renderArtifacts, repositoryFileErrors, openRepositoryFile };
}
function withMapValue(map: Map<string, string>, key: string, value: string): Map<string, string> {
  const next = new Map(map);
  next.set(key, value);
  return next;
}

function withoutMapKey(map: Map<string, string>, key: string): Map<string, string> {
  if (!map.has(key)) return map;
  const next = new Map(map);
  next.delete(key);
  return next;
}
