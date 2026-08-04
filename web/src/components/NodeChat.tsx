import {
  AlertTriangle,
  Download,
  ExternalLink,
  File,
  History,
  Inbox,
  LoaderCircle,
  MessageCircle,
  Play,
  RadioTower,
  RotateCcw,
  Send,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  artifactUrl,
  chatMessageTranscriptLine,
  chatTasksMissingFromHistory,
  isActiveTask,
  latestNativeSessionId,
  orderTranscriptLines,
  reconstructTaskTranscript,
  relatedChatTasks,
  resumablePausedChatTask,
  taskKindLabel,
} from "../agentTasks";
import {
  chatDraftStorageKey,
  chatModeStorageKey,
  isConversationModeShortcut,
  latestPersistedChatConfig,
  latestPersistedConversationMode,
  parseConversationMode,
  toggleConversationMode,
} from "../chatWorkspace";
import { MarkdownAnswer } from "../chatMarkdown";
import type { GlossaryIndex } from "../glossary";
import {
  downloadDesktopArtifact,
  isDesktopRuntime,
  openDesktopArtifactPreview,
} from "../desktopRuntime";
import type {
  AgentArtifactDescriptor,
  AgentTask,
  ChatMessage,
  ConversationMode,
  GraphNode,
  GraphUpdateResult,
  ProjectSnapshot,
  StartAgentTask,
  WatcherRecord,
} from "../types";
import {
  CHAT_SCROLL_BOTTOM_TOLERANCE_PX,
  CHAT_USER_MESSAGE_COLLAPSE_THRESHOLD,
} from "../uiConstants";
import { profileRunConfig } from "./AgentConfigControls";
import { SkillPicker, useSkillPicker } from "./SkillPicker";
import { RepositoryScope } from "./RepositoryScope";

interface Props {
  project: ProjectSnapshot;
  node?: GraphNode | null;
  nodes?: Readonly<Record<string, GraphNode>>;
  glossaryIndex?: GlossaryIndex;
  conversationTitle?: string;
  runScope: string[];
  tasks: AgentTask[];
  watchers?: WatcherRecord[];
  historyMessages?: ChatMessage[];
  chatId: string;
  presentation?: "floating" | "workspace";
  reviewPending?: boolean;
  graphChangesDisabled?: boolean;
  onStartTask: StartAgentTask;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onRepairGraphUpdate: (taskId: string) => Promise<void>;
  onOpenNode?: (nodeId: string) => void;
  onStopWatcher?: (watcherId: string) => void;
  onClose: () => void;
  onResumeTask: (task: AgentTask) => void;
  onRetryTask: (task: AgentTask) => void;
}

interface PendingChatTurn {
  clientId: string;
  text: string;
  timestamp: string;
  mode: ConversationMode;
}

export function NodeChat({
  project,
  node,
  nodes = {},
  glossaryIndex,
  conversationTitle,
  runScope,
  tasks,
  watchers = [],
  historyMessages = [],
  chatId,
  presentation = "floating",
  reviewPending = false,
  graphChangesDisabled = false,
  onStartTask,
  onInspectTask,
  onOpenInbox,
  onRepairGraphUpdate,
  onOpenNode,
  onStopWatcher,
  onClose,
  onResumeTask,
  onRetryTask,
}: Props) {
  const surface = node ? "node_chat" : "project_chat";
  const skillCatalog = project.skill_catalog ?? [];
  const skillDefaults = project.skill_defaults ?? { workflow_ids: [], skill_ids: [] };
  const relatedTasks = useMemo(
    () => relatedChatTasks(tasks, surface, node?.id, chatId),
    [chatId, node?.id, surface, tasks],
  );
  const [pendingTurn, setPendingTurn] = useState<PendingChatTurn | null>(null);
  const transcript = useMemo(
    () =>
      orderTranscriptLines([
        ...historyMessages.map(chatMessageTranscriptLine),
        ...reconstructTaskTranscript(chatTasksMissingFromHistory(relatedTasks, historyMessages)),
        ...(pendingTurn
          ? [
              {
                role: "human" as const,
                text: pendingTurn.text,
                taskId: pendingTurn.clientId,
                timestamp: pendingTurn.timestamp,
                mode: pendingTurn.mode,
                trigger: "human" as const,
              },
            ]
          : []),
      ]),
    [historyMessages, pendingTurn, relatedTasks],
  );
  const config = useMemo(
    () =>
      latestPersistedChatConfig(
        historyMessages,
        relatedTasks,
        profileRunConfig(project.agent_profiles[surface]),
      ),
    [historyMessages, project.agent_profiles, relatedTasks, surface],
  );
  const [scope, setScope] = useState(runScope);
  const draftKey = chatDraftStorageKey(project.id, chatId);
  const modeKey = chatModeStorageKey(project.id, chatId);
  const derivedMode = useMemo(
    () => latestPersistedConversationMode(historyMessages, relatedTasks),
    [historyMessages, relatedTasks],
  );
  const [message, setMessage] = useState(() => readStorage(draftKey) ?? "");
  const [modeState, setModeState] = useState<{ value: ConversationMode; pinned: boolean }>(() => {
    const storedMode = parseConversationMode(readStorage(modeKey));
    return { value: storedMode ?? derivedMode, pinned: Boolean(storedMode) };
  });
  const [submitting, setSubmitting] = useState(false);
  const [expandedHumanMessageIds, setExpandedHumanMessageIds] = useState<Set<string>>(
    () => new Set(),
  );
  const chatLinesRef = useRef<HTMLDivElement | null>(null);
  const shouldStickToBottomRef = useRef(true);
  const lastChatIdRef = useRef(chatId);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [repairingTaskId, setRepairingTaskId] = useState<string | null>(null);
  const [repairErrors, setRepairErrors] = useState<Map<string, string>>(() => new Map());
  const [unavailableArtifacts, setUnavailableArtifacts] = useState<Set<string>>(() => new Set());
  const [artifactShellErrors, setArtifactShellErrors] = useState<Map<string, string>>(
    () => new Map(),
  );
  const skills = useSkillPicker({
    catalog: skillCatalog,
    defaults: skillDefaults,
  });
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const relatedActive = relatedTasks.some(isActiveTask);
  const liveWatchers = useMemo(
    () =>
      watchers.filter((watcher) => watcher.chat_id === chatId && watcher.status !== "completed"),
    [chatId, watchers],
  );
  const continuedTaskIds = useMemo(
    () =>
      new Set(
        relatedTasks.flatMap((task) =>
          task.parent_operation_id ? [task.parent_operation_id] : [],
        ),
      ),
    [relatedTasks],
  );
  const pausedAttempt = resumablePausedChatTask(relatedTasks);
  const readiness = project.provider_readiness[config.run_on]?.[config.provider];
  const providerReady =
    readiness === undefined || Boolean(readiness.installed && readiness.authenticated);
  const sessionId =
    latestNativeSessionId(relatedTasks) ??
    [...historyMessages].reverse().find((message) => message.native_session_id)
      ?.native_session_id ??
    null;
  const mode = modeState.value;
  const chatTitle = node?.title || conversationTitle || project.name;

  useEffect(() => {
    setModeState((current) =>
      current.pinned || current.value === derivedMode
        ? current
        : { ...current, value: derivedMode },
    );
  }, [derivedMode]);

  useEffect(() => {
    skills.reset();
    // Settings supplies fresh conversation defaults; an open turn keeps its own.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatId, project.id]);

  useEffect(() => {
    if (message) writeStorage(draftKey, message);
    else removeStorage(draftKey);
  }, [draftKey, message]);

  useEffect(() => {
    if (lastChatIdRef.current !== chatId) {
      lastChatIdRef.current = chatId;
      shouldStickToBottomRef.current = true;
    }
    const element = chatLinesRef.current;
    if (!element || !shouldStickToBottomRef.current) return;
    element.scrollTop = element.scrollHeight;
  }, [chatId, transcript]);

  const selectMode = useCallback(
    (next: ConversationMode) => {
      writeStorage(modeKey, next);
      setModeState({ value: next, pinned: true });
    },
    [modeKey],
  );

  const toggleMode = useCallback(() => {
    setModeState((current) => {
      const next = toggleConversationMode(current.value);
      writeStorage(modeKey, next);
      return { value: next, pinned: true };
    });
  }, [modeKey]);

  useEffect(() => {
    if (presentation !== "workspace") return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat) return;
      if (!isConversationModeShortcut(event.key, event.shiftKey)) return;
      event.preventDefault();
      toggleMode();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [presentation, toggleMode]);

  const updateMessage = (next: string) => {
    if (next && !modeState.pinned) {
      writeStorage(modeKey, mode);
      setModeState({ value: mode, pinned: true });
    }
    setMessage(next);
    skills.readMessage(next);
    setSubmitError(null);
  };

  const toggleHumanMessage = (messageId: string) => {
    setExpandedHumanMessageIds((current) => {
      const next = new Set(current);
      if (next.has(messageId)) next.delete(messageId);
      else next.add(messageId);
      return next;
    });
  };

  const handleChatScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    shouldStickToBottomRef.current =
      element.scrollHeight - element.scrollTop - element.clientHeight <=
      CHAT_SCROLL_BOTTOM_TOLERANCE_PX;
  };

  const send = async () => {
    const text = message.trim();
    if (!text || relatedActive || pausedAttempt || submitting || repairingTaskId || reviewPending)
      return;
    shouldStickToBottomRef.current = true;
    const clientId = `pending-${crypto.randomUUID()}`;
    setPendingTurn({
      clientId,
      text,
      timestamp: new Date().toISOString(),
      mode,
    });
    setMessage("");
    setSubmitError(null);
    setSubmitting(true);
    try {
      await onStartTask(surface, {
        ...config,
        model: config.model || null,
        run_truth_scope: scope,
        node_id: node?.id ?? null,
        message: text,
        chat_id: chatId,
        session_id: sessionId,
        mode,
        invoked_workflow_ids: skills.selection.workflow_ids,
        invoked_skill_ids: skills.selection.skill_ids,
      });
      setPendingTurn((current) => (current?.clientId === clientId ? null : current));
      skills.reset();
      selectMode(mode);
    } catch (error) {
      setPendingTurn((current) => (current?.clientId === clientId ? null : current));
      setMessage((current) => (current ? current : text));
      setSubmitError(error instanceof Error ? error.message : String(error));
    } finally {
      setSubmitting(false);
    }
  };

  const repairGraphUpdate = async (taskId: string) => {
    if (repairingTaskId) return;
    setRepairingTaskId(taskId);
    setRepairErrors((current) => withoutMapKey(current, taskId));
    try {
      await onRepairGraphUpdate(taskId);
    } catch (error) {
      setRepairErrors((current) =>
        withMapValue(current, taskId, error instanceof Error ? error.message : String(error)),
      );
    } finally {
      setRepairingTaskId(null);
    }
  };

  const markArtifactUnavailable = (taskId: string, artifactId: string) => {
    setUnavailableArtifacts((current) => {
      const next = new Set(current);
      next.add(`${taskId}:${artifactId}`);
      return next;
    });
  };

  const checkArtifact = async (
    taskId: string,
    artifact: AgentArtifactDescriptor,
    action: "preview" | "download",
  ) => {
    const url = artifactUrl(project.id, taskId, artifact.artifact_id, action);
    if (!(await artifactIsAvailable(url))) {
      markArtifactUnavailable(taskId, artifact.artifact_id);
    }
  };

  const openArtifact = async (taskId: string, artifact: AgentArtifactDescriptor) => {
    if (desktop) {
      const key = `${taskId}:${artifact.artifact_id}`;
      setArtifactShellErrors((current) => withoutMapKey(current, key));
      try {
        await openDesktopArtifactPreview({
          projectId: project.id,
          taskId,
          artifactId: artifact.artifact_id,
        });
      } catch (error) {
        setArtifactShellErrors((current) =>
          withMapValue(
            current,
            key,
            `Open failed: ${error instanceof Error ? error.message : String(error)}`,
          ),
        );
      }
      return;
    }
    const target = window.open("about:blank", "_blank");
    if (!target) {
      markArtifactUnavailable(taskId, artifact.artifact_id);
      return;
    }
    target.opener = null;
    const url = artifactUrl(project.id, taskId, artifact.artifact_id, "preview");
    if (!(await artifactIsAvailable(url))) {
      target.close();
      markArtifactUnavailable(taskId, artifact.artifact_id);
      return;
    }
    try {
      target.location.replace(url);
    } catch {
      target.close();
      markArtifactUnavailable(taskId, artifact.artifact_id);
    }
  };

  const downloadArtifact = async (taskId: string, artifact: AgentArtifactDescriptor) => {
    const key = `${taskId}:${artifact.artifact_id}`;
    setArtifactShellErrors((current) => withoutMapKey(current, key));
    try {
      await downloadDesktopArtifact({
        projectId: project.id,
        taskId,
        artifactId: artifact.artifact_id,
        suggestedName: artifact.name,
      });
    } catch (error) {
      setArtifactShellErrors((current) =>
        withMapValue(
          current,
          key,
          `Download failed: ${error instanceof Error ? error.message : String(error)}`,
        ),
      );
    }
  };

  return (
    <div
      className={`chat-dock ${presentation}`}
      data-mode={mode}
      role={presentation === "floating" ? "dialog" : "region"}
      aria-modal="false"
      aria-label={node || conversationTitle ? `Chat about ${chatTitle}` : "Project chat"}
      aria-keyshortcuts={presentation === "workspace" ? "Shift+Tab" : undefined}
    >
      {presentation === "floating" && (
        <header data-drag-handle="true">
          <MessageCircle size={17} />
          <strong>{chatTitle}</strong>
          {liveWatchers.length > 0 && (
            <span className="chat-watcher-count">
              <RadioTower size={12} /> {liveWatchers.length}
            </span>
          )}
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Minimize chat; background work will continue"
          >
            <X size={17} />
          </button>
        </header>
      )}
      <div className="chat-context-controls">
        <div
          className="agent-provider-label"
          aria-busy={readiness === undefined}
          aria-label={`Chat provider: ${readiness?.label || config.provider}`}
        >
          {readiness?.label || config.provider}
          {readiness === undefined && (
            <LoaderCircle className="spin" size={12} aria-label="Checking provider" />
          )}
        </div>
        <div className="chat-scope-control">
          <span>Raw truth inputs</span>
          <RepositoryScope
            repositories={project.repositories}
            projectScope={project.project_truth_scope}
            stateRepository={project.state_repository}
            selected={scope}
            onChange={relatedActive || reviewPending ? () => undefined : setScope}
          />
        </div>
      </div>
      {liveWatchers.length > 0 && (
        <section className="chat-watchers" aria-label="Active watchers">
          <header>
            <RadioTower size={13} />
            <strong>
              {liveWatchers.length} active watcher{liveWatchers.length === 1 ? "" : "s"}
            </strong>
          </header>
          {liveWatchers.map((watcher) => (
            <div className={`chat-watcher-row ${watcher.status}`} key={watcher.watcher_id}>
              <strong>{fileName(watcher.log_path)}</strong>
              <time dateTime={watcher.last_checked_at ?? undefined}>
                {watcher.last_checked_at
                  ? `Checked ${new Date(watcher.last_checked_at).toLocaleString()}`
                  : "Not checked yet"}
              </time>
              {watcher.last_error && <span role="alert">{watcher.last_error}</span>}
              {onStopWatcher && (
                <button
                  className="button compact"
                  type="button"
                  onClick={() => onStopWatcher(watcher.watcher_id)}
                >
                  Stop watching
                </button>
              )}
            </div>
          ))}
        </section>
      )}
      <div
        className="node-chat-lines"
        aria-live="polite"
        onScroll={handleChatScroll}
        ref={chatLinesRef}
      >
        {transcript.map((line, index) => {
          const messageId = `${line.taskId}:${index}`;
          const task = relatedTasks.find((candidate) => candidate.operation_id === line.taskId);
          const activeLineTask = task && isActiveTask(task) ? task : null;
          const pausedLineTask =
            task?.status === "paused" && task.can_resume && !continuedTaskIds.has(task.operation_id)
              ? task
              : null;
          const pendingLine = pendingTurn?.clientId === line.taskId;
          const collapsible =
            line.role === "human" && line.text.length > CHAT_USER_MESSAGE_COLLAPSE_THRESHOLD;
          const expanded = expandedHumanMessageIds.has(messageId);
          return (
            <div className={`node-chat-line ${line.role}`} key={`${line.taskId}-${index}`}>
              {line.role === "human" && line.mode && (
                <span className={`chat-turn-mode ${line.mode}`}>{modeLabel(line.mode)}</span>
              )}
              {line.trigger === "watcher" && (
                <span className="chat-turn-trigger watcher">Watcher</span>
              )}
              {line.role === "agent" ? (
                line.text && (
                  <div className="chat-markdown">
                    <MarkdownAnswer
                      text={line.text}
                      nodes={nodes}
                      glossaryIndex={glossaryIndex}
                      onOpenNode={onOpenNode}
                    />
                  </div>
                )
              ) : line.role === "human" ? (
                <>
                  <div
                    className={`chat-human-message${collapsible && !expanded ? " collapsed" : ""}`}
                  >
                    <span className="node-chat-text">{line.text}</span>
                  </div>
                  {collapsible && (
                    <button
                      type="button"
                      className="chat-message-toggle"
                      aria-expanded={expanded}
                      onClick={() => toggleHumanMessage(messageId)}
                    >
                      {expanded ? "See less" : "See more"}
                    </button>
                  )}
                  {pausedLineTask ? (
                    <InlinePausedTask
                      task={pausedLineTask}
                      onResume={() => onResumeTask(pausedLineTask)}
                      onRetry={() => onRetryTask(pausedLineTask)}
                    />
                  ) : activeLineTask ? (
                    <InlineTaskProgress task={activeLineTask} />
                  ) : pendingLine ? (
                    <InlineTaskProgress task={null} />
                  ) : null}
                </>
              ) : pausedLineTask ? null : (
                <span className="node-chat-text">{line.text}</span>
              )}
              {line.artifacts?.map((artifact) => {
                const unavailable = unavailableArtifacts.has(
                  `${line.taskId}:${artifact.artifact_id}`,
                );
                const shellError = artifactShellErrors.get(
                  `${line.taskId}:${artifact.artifact_id}`,
                );
                return (
                  <div
                    className={`chat-artifact${unavailable ? " unavailable" : ""}`}
                    key={artifact.artifact_id}
                  >
                    <File size={14} />
                    <span>{artifact.name}</span>
                    {unavailable ? (
                      <strong>Preview unavailable</strong>
                    ) : (
                      <div className="chat-artifact-actions">
                        <button
                          type="button"
                          onClick={() => void openArtifact(line.taskId, artifact)}
                        >
                          <ExternalLink size={12} /> Open
                        </button>
                        {desktop ? (
                          <button
                            type="button"
                            onClick={() => void downloadArtifact(line.taskId, artifact)}
                          >
                            <Download size={12} /> Download
                          </button>
                        ) : (
                          <a
                            href={artifactUrl(
                              project.id,
                              line.taskId,
                              artifact.artifact_id,
                              "download",
                            )}
                            download={artifact.name}
                            onClick={() => void checkArtifact(line.taskId, artifact, "download")}
                          >
                            <Download size={12} /> Download
                          </a>
                        )}
                      </div>
                    )}
                    {shellError && (
                      <strong className="chat-artifact-shell-error" role="alert">
                        {shellError}
                      </strong>
                    )}
                  </div>
                );
              })}
              {line.role === "agent" && line.graphUpdate && (
                <GraphUpdateReceipt
                  update={line.graphUpdate}
                  taskId={line.taskId}
                  repairBusy={repairingTaskId === line.taskId}
                  repairDisabled={
                    graphChangesDisabled || relatedActive || submitting || reviewPending
                  }
                  repairContinued={continuedTaskIds.has(line.taskId)}
                  repairError={repairErrors.get(line.taskId) ?? null}
                  onInspectTask={onInspectTask}
                  onOpenInbox={onOpenInbox}
                  onRepair={() => void repairGraphUpdate(line.taskId)}
                />
              )}
            </div>
          );
        })}
        {submitError && <div className="node-chat-line error">{submitError}</div>}
      </div>
      <div className="chat-composer" data-mode={mode}>
        <SkillPicker {...skills.props} />
        <textarea
          aria-label="Message"
          aria-keyshortcuts="Shift+Tab"
          value={message}
          onChange={(event) => updateMessage(event.target.value)}
          onKeyDown={(event) => {
            if (skills.handleKeyDown(event)) return;
            if (isConversationModeShortcut(event.key, event.shiftKey)) {
              if (presentation !== "workspace") {
                event.preventDefault();
                toggleMode();
              }
              return;
            }
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <div className="chat-send">
          <div className="chat-mode-toggle" role="group" aria-label="Conversation mode">
            {(["discuss", "work"] as const).map((option) => (
              <button
                type="button"
                className={option}
                aria-pressed={mode === option}
                onClick={() => selectMode(option)}
                key={option}
              >
                {modeLabel(option)}
              </button>
            ))}
          </div>
          <button
            className="icon-button primary chat-send-button"
            disabled={
              !message.trim() ||
              relatedActive ||
              Boolean(pausedAttempt) ||
              submitting ||
              Boolean(repairingTaskId) ||
              reviewPending ||
              scope.length === 0 ||
              !providerReady
            }
            onClick={() => void send()}
            aria-label={`Start ${modeLabel(mode)} turn`}
          >
            <Send size={15} />
          </button>
        </div>
      </div>
    </div>
  );
}

function InlineTaskProgress({ task }: { task: AgentTask | null }) {
  const label = task
    ? task.status_message || `${taskKindLabel(task.kind)} is running`
    : "Starting task";
  return (
    <div className="chat-task-inline running" role="status" aria-label="Agent task running">
      <LoaderCircle className="spin" size={12} />
      <span>{label}</span>
      <div className="chat-task-progress" role="progressbar" aria-label="Task in progress">
        <span style={{ width: `${Math.round((task?.progress ?? 0) * 100)}%` }} />
      </div>
    </div>
  );
}

function InlinePausedTask({
  task,
  onResume,
  onRetry,
}: {
  task: AgentTask;
  onResume: () => void;
  onRetry: () => void;
}) {
  return (
    <div className="chat-task-inline paused" role="status" aria-label="Agent task paused">
      <span>{task.status_message}</span>
      <button type="button" className="button compact primary" onClick={onResume}>
        <Play size={11} /> Resume
      </button>
      <button type="button" className="button compact secondary" onClick={onRetry}>
        <RotateCcw size={11} /> Retry
      </button>
    </div>
  );
}

function GraphUpdateReceipt({
  update,
  taskId,
  repairBusy,
  repairDisabled,
  repairContinued,
  repairError,
  onInspectTask,
  onOpenInbox,
  onRepair,
}: {
  update: GraphUpdateResult;
  taskId: string;
  repairBusy: boolean;
  repairDisabled: boolean;
  repairContinued: boolean;
  repairError: string | null;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onRepair: () => void;
}) {
  if (update.status === "none") return null;
  const proposalCount = update.proposal_ids.length;
  return (
    <div className={`chat-graph-receipt ${update.status}`}>
      <div className="chat-graph-receipt-actions">
        {update.status === "applied" && (
          <button type="button" onClick={() => onInspectTask(taskId)}>
            <History size={12} />
            {update.applied_revision === null
              ? "Graph updated"
              : `Graph updated · r${update.applied_revision}`}
          </button>
        )}
        {update.status === "rejected" && (
          <strong>
            <AlertTriangle size={12} /> Graph update rejected
          </strong>
        )}
        {update.status === "applied" && proposalCount > 0 && (
          <button type="button" onClick={onOpenInbox}>
            <Inbox size={12} />
            {proposalCount} proposal{proposalCount === 1 ? "" : "s"} sent to Inbox
          </button>
        )}
        {update.status === "rejected" && update.repairable && !repairContinued && (
          <button type="button" disabled={repairBusy || repairDisabled} onClick={onRepair}>
            <RotateCcw className={repairBusy ? "spin" : undefined} size={12} />
            Repair graph update
          </button>
        )}
      </div>
      {update.change_summary.length > 0 && (
        <ul className="chat-graph-change-summary">
          {update.change_summary.map((item, index) => (
            <li key={`${index}:${item}`}>{item}</li>
          ))}
        </ul>
      )}
      {update.status === "rejected" && update.validation_messages.length > 0 && (
        <ul className="chat-graph-validation">
          {update.validation_messages.map((item, index) => (
            <li key={`${index}:${item}`}>{item}</li>
          ))}
        </ul>
      )}
      {repairError && (
        <strong className="chat-graph-repair-error" role="alert">
          {repairError}
        </strong>
      )}
    </div>
  );
}

function modeLabel(mode: ConversationMode): "Discuss" | "Work" {
  return mode === "discuss" ? "Discuss" : "Work";
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {}
}

function removeStorage(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {}
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

async function artifactIsAvailable(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { method: "HEAD" });
    return response.ok;
  } catch {
    return false;
  }
}
