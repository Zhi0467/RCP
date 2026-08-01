import {
  AlertTriangle,
  Download,
  ExternalLink,
  File,
  History,
  Inbox,
  MessageCircle,
  RotateCcw,
  Send,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  artifactUrl,
  chatMessageTranscriptLine,
  chatTasksMissingFromHistory,
  isActiveTask,
  latestNativeSessionId,
  reconstructTaskTranscript,
  relatedChatTasks,
  resumablePausedChatTask,
  taskKindLabel,
} from "../agentTasks";
import {
  chatDraftStorageKey,
  chatModeStorageKey,
  isConversationModeShortcut,
  latestPersistedConversationMode,
  parseConversationMode,
  toggleConversationMode,
} from "../chatWorkspace";
import { MarkdownAnswer } from "../chatMarkdown";
import {
  downloadDesktopArtifact,
  isDesktopRuntime,
  openDesktopArtifactPreview,
} from "../desktopRuntime";
import type {
  AgentArtifactDescriptor,
  AgentRunConfig,
  AgentTask,
  ChatMessage,
  ConversationMode,
  GraphNode,
  GraphUpdateResult,
  ProjectSnapshot,
  StartAgentTask,
} from "../types";
import { AgentConfigControls, profileRunConfig } from "./AgentConfigControls";
import { RepositoryScope } from "./RepositoryScope";

interface Props {
  project: ProjectSnapshot;
  node?: GraphNode | null;
  runScope: string[];
  tasks: AgentTask[];
  activeTask: AgentTask | null;
  historyMessages?: ChatMessage[];
  chatId: string;
  presentation?: "floating" | "workspace";
  reviewPending?: boolean;
  graphChangesDisabled?: boolean;
  onStartTask: StartAgentTask;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onRepairGraphUpdate: (taskId: string) => Promise<void>;
  onClose: () => void;
}

export function NodeChat({
  project,
  node,
  runScope,
  tasks,
  activeTask,
  historyMessages = [],
  chatId,
  presentation = "floating",
  reviewPending = false,
  graphChangesDisabled = false,
  onStartTask,
  onInspectTask,
  onOpenInbox,
  onRepairGraphUpdate,
  onClose,
}: Props) {
  const surface = node ? "node_chat" : "project_chat";
  const relatedTasks = useMemo(
    () => relatedChatTasks(tasks, surface, node?.id, chatId),
    [chatId, node?.id, surface, tasks],
  );
  const transcript = useMemo(
    () => [
      ...historyMessages.map(chatMessageTranscriptLine),
      ...reconstructTaskTranscript(chatTasksMissingFromHistory(relatedTasks, historyMessages)),
    ],
    [historyMessages, relatedTasks],
  );
  const [config, setConfig] = useState<AgentRunConfig>(() =>
    profileRunConfig(project.agent_profiles[surface]),
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
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [repairingTaskId, setRepairingTaskId] = useState<string | null>(null);
  const [repairErrors, setRepairErrors] = useState<Map<string, string>>(() => new Map());
  const [unavailableArtifacts, setUnavailableArtifacts] = useState<Set<string>>(() => new Set());
  const [artifactShellErrors, setArtifactShellErrors] = useState<Map<string, string>>(
    () => new Map(),
  );
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const relatedActive = relatedTasks.some(isActiveTask);
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
  const locked = transcript.some((line) => line.role === "human");
  const readiness = project.provider_readiness[config.run_on]?.[config.provider];
  const providerReady = Boolean(readiness?.installed && readiness?.authenticated);
  const sessionId =
    latestNativeSessionId(relatedTasks) ??
    [...historyMessages].reverse().find((message) => message.native_session_id)
      ?.native_session_id ??
    null;
  const mode = modeState.value;

  useEffect(() => {
    setModeState((current) =>
      current.pinned || current.value === derivedMode
        ? current
        : { ...current, value: derivedMode },
    );
  }, [derivedMode]);

  useEffect(() => {
    if (message) writeStorage(draftKey, message);
    else removeStorage(draftKey);
  }, [draftKey, message]);

  const selectMode = (next: ConversationMode) => {
    writeStorage(modeKey, next);
    setModeState({ value: next, pinned: true });
  };

  const updateMessage = (next: string) => {
    if (next && !modeState.pinned) {
      writeStorage(modeKey, mode);
      setModeState({ value: mode, pinned: true });
    }
    setMessage(next);
    setSubmitError(null);
  };

  const send = async () => {
    const text = message.trim();
    if (!text || activeTask || pausedAttempt || submitting || repairingTaskId || reviewPending)
      return;
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
      });
      setMessage("");
      selectMode(mode);
    } catch (error) {
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
      aria-label={node ? `Chat about ${node.title}` : "Project chat"}
    >
      <header data-drag-handle={presentation === "floating" ? "true" : undefined}>
        <MessageCircle size={17} />
        <strong>{node?.title || project.name}</strong>
        {presentation === "floating" && (
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Minimize chat; background work will continue"
          >
            <X size={17} />
          </button>
        )}
      </header>
      <AgentConfigControls
        project={project}
        value={config}
        onChange={setConfig}
        runOnLocked
        locked={locked || Boolean(activeTask) || reviewPending}
        compact
        collapsible
        defaultCollapsed
      >
        <div className="chat-scope-control">
          <span>Raw truth inputs</span>
          <RepositoryScope
            repositories={project.repositories}
            projectScope={project.project_truth_scope}
            stateRepository={project.state_repository}
            selected={scope}
            onChange={locked || activeTask || reviewPending ? () => undefined : setScope}
          />
        </div>
      </AgentConfigControls>
      <div className="node-chat-lines" aria-live="polite">
        {transcript.map((line, index) => (
          <div className={`node-chat-line ${line.role}`} key={`${line.taskId}-${index}`}>
            {line.role === "human" && line.mode && (
              <span className={`chat-turn-mode ${line.mode}`}>{modeLabel(line.mode)}</span>
            )}
            {line.role === "agent" ? (
              line.text && (
                <div className="chat-markdown">
                  <MarkdownAnswer text={line.text} />
                </div>
              )
            ) : (
              <span className="node-chat-text">{line.text}</span>
            )}
            {line.artifacts?.map((artifact) => {
              const unavailable = unavailableArtifacts.has(
                `${line.taskId}:${artifact.artifact_id}`,
              );
              const shellError = artifactShellErrors.get(`${line.taskId}:${artifact.artifact_id}`);
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
                  graphChangesDisabled || Boolean(activeTask) || submitting || reviewPending
                }
                repairContinued={continuedTaskIds.has(line.taskId)}
                repairError={repairErrors.get(line.taskId) ?? null}
                onInspectTask={onInspectTask}
                onOpenInbox={onOpenInbox}
                onRepair={() => void repairGraphUpdate(line.taskId)}
              />
            )}
          </div>
        ))}
        {submitError && <div className="node-chat-line error">{submitError}</div>}
        {relatedActive && (
          <div className="thinking">
            <i />
            <i />
            <i /> {activeTask?.status_message || `${taskKindLabel(surface)} is running`}
          </div>
        )}
        {pausedAttempt && (
          <div className="chat-task-blocked">
            This conversation has a paused attempt. Resume it from the task banner, or use Retry
            before sending another turn.
          </div>
        )}
        {activeTask && !relatedActive && (
          <div className="chat-task-blocked">
            Another agent task is active. This conversation remains usable and can continue when
            that task finishes.
          </div>
        )}
      </div>
      <div className="chat-composer" data-mode={mode}>
        <textarea
          aria-label="Message"
          aria-keyshortcuts="Shift+Tab"
          value={message}
          onChange={(event) => updateMessage(event.target.value)}
          onKeyDown={(event) => {
            if (isConversationModeShortcut(event.key, event.shiftKey)) {
              event.preventDefault();
              selectMode(toggleConversationMode(mode));
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
            <kbd aria-label="Shift plus Tab">⇧⇥</kbd>
          </div>
          <button
            className="icon-button primary chat-send-button"
            disabled={
              !message.trim() ||
              Boolean(activeTask) ||
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
        {proposalCount > 0 && (
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
