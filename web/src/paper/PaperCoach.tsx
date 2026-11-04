import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import {
  History,
  ListChecks,
  LoaderCircle,
  MessageCirclePlus,
  RotateCcw,
  Send,
  Square,
  Zap,
} from "lucide-react";
import {
  coachConversation,
  coachSteersAfterPrompts,
  isActiveTask,
  type TaskTranscriptLine,
} from "../agents/agentTasks";
import { AgentWindowTranscript } from "../agentWindow/AgentWindowTranscript";
import { projectAgentWindowEndpoints } from "../agentWindow/agentWindowEndpoints";
import { useAgentWindowArtifacts } from "../agentWindow/useAgentWindowArtifacts";
import { useAgentWindowLines } from "../agentWindow/useAgentWindowLines";
import { panelTools } from "./panelTools";
import { launchProviderReady, profileRunConfig } from "../core/AgentConfigControls";
import { useAgentConfigPicker } from "../core/AgentConfigPicker";
import { api } from "../core/api";
import { readPref, writePref } from "../core/prefStorage";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { useResource } from "../core/useResource";
import { paperSessionTasksProfile } from "../agents/taskResources";
import { returnSends } from "../ui/composerKeys";
import { useSkillPicker } from "../core/SkillPicker";
import { EMPTY_SKILL_SELECTION, skillInvocationFields } from "../core/skillPickerModel";
import type {
  AgentQuestion,
  AgentRunConfig,
  AgentTask,
  ChatMessage,
  ProjectSnapshot,
  StartAgentTask,
  WritingSession,
} from "../core/types";
import { taskRetryRequestBody } from "../taskRecovery";
import { ChatComposer, ComposerField } from "../chat/ChatComposer";
import { ProviderSwitchHint } from "../chat/ProviderSwitchHint";
import { AgentStatusStrip } from "../chat/AgentStatusStrip";
import { useComposerAttachments } from "../chat/composerAttachments";
import type { AnswerComments } from "../chat/answerComments";
import { assembleChatTurn } from "../chat/chatInput";
import { PaperChanges, type PaperChange, type PaperChangeAction } from "./PaperChanges";
import "./paper-coach.css";

const NO_QUESTIONS: AgentQuestion[] = [];

/** A session's agent pick; the coach always runs on its profile's machine, the base. */
function sessionConfig(session: WritingSession, baseMachine: string): AgentRunConfig {
  return {
    provider: session.provider,
    model: session.model === "provider-default" ? "" : session.model,
    reasoning: session.reasoning ?? "medium",
    run_on: baseMachine,
  };
}

export function PaperCoach({
  apiBase,
  project,
  tasks,
  onStartTask,
  changes,
  onChangeAction,
  onUndoAll,
  onLocate,
  toolbar,
  comments,
  submitting,
  setSubmitting,
  error,
  setError,
  transcriptRef,
  textarea,
}: {
  apiBase: string;
  project: ProjectSnapshot;
  tasks: AgentTask[];
  onStartTask: StartAgentTask;
  changes: PaperChange[];
  onChangeAction: (groupId: string, action: PaperChangeAction) => Promise<void>;
  onUndoAll: (taskId: string) => Promise<void>;
  onLocate: (change: PaperChange) => void;
  /** Where History and New chat sit: the Paper view row, while the coach shows. */
  toolbar: HTMLElement | null;
  /** Comments the coach and the preview stage for the next turn; the workspace owns them. */
  comments: AnswerComments;
  submitting: boolean;
  setSubmitting: (value: boolean) => void;
  error: string | null;
  setError: (value: string | null) => void;
  transcriptRef: RefObject<HTMLDivElement | null>;
  textarea: RefObject<HTMLTextAreaElement | null>;
}) {
  const baseMachine = profileRunConfig(project.agent_profiles.paper_coach).run_on;
  const [sessions, setSessions] = useState<WritingSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [fresh, setFresh] = useState(false);
  // Turns started here, kept from admission until the task list carries them.
  const [started, setStarted] = useState<AgentTask[]>([]);
  // The human's pick for the next turns, kept while the same conversation stays open.
  const [configOverride, setConfigOverride] = useState<{
    anchor: string | null;
    config: AgentRunConfig;
  } | null>(null);
  const [message, setMessage] = useState("");
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [sessionRefresh, setSessionRefresh] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyButton = useRef<HTMLButtonElement>(null);
  const historyList = useRef<HTMLDivElement>(null);
  useLayerDismiss(
    historyOpen,
    () => setHistoryOpen(false),
    [historyList, historyButton],
    historyButton,
  );
  const modeKey = `rcp:paper-coach-mode:${apiBase}`;
  const [applyDirectly, setApplyDirectly] = useState(() => readPref(modeKey) === "apply");
  const text = assembleChatTurn(message, comments.annotations);
  const composer = useComposerAttachments({
    project,
    referencesKey: null,
    input: textarea,
    message: { get: () => message, set: setMessage },
    onError: setError,
  });
  const taskRevision = tasks
    .filter((task) => task.kind === "paper_coach")
    .map((task) => `${task.operation_id}:${task.updated_at}`)
    .join("|");
  useEffect(() => {
    const controller = new AbortController();
    api<WritingSession[]>(`${apiBase}/paper/sessions`, { signal: controller.signal })
      .then((next) => {
        setSessions(next);
        setSessionError(null);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setSessionError(String(cause));
      });
    return () => controller.abort();
  }, [apiBase, taskRevision, sessionRefresh]);
  const attach = (session: WritingSession) => {
    setSessionId(session.native_session_id);
    setFresh(false);
    setStarted([]);
    setConfigOverride(null);
    setHistoryOpen(false);
    setError(null);
  };
  const knownTasks = useMemo(
    () => [
      ...tasks,
      ...started.filter((turn) => !tasks.some((task) => task.operation_id === turn.operation_id)),
    ],
    [tasks, started],
  );
  const openedSession =
    sessionId ??
    (!fresh
      ? (knownTasks.find((task) => task.kind === "paper_coach" && task.native_session_id)
          ?.native_session_id ??
        sessions[0]?.native_session_id ??
        null)
      : null);
  const sessionRead = useResource(
    `${project.id}:paper-coach`,
    openedSession ? paperSessionTasksProfile(project.id, openedSession) : null,
  );
  const mergedTasks = useMemo(() => {
    const byId = new Map(knownTasks.map((task) => [task.operation_id, task]));
    for (const task of sessionRead.value ?? []) {
      const known = byId.get(task.operation_id);
      if (!known || known.updated_at <= task.updated_at) byId.set(task.operation_id, task);
    }
    return [...byId.values()];
  }, [knownTasks, sessionRead.value]);
  const conversation = coachConversation(
    mergedTasks,
    openedSession,
    started.map((task) => task.operation_id),
  );
  // The session the next turn resumes: the newest turn's, as in chat.
  const currentSessionId = conversation.sessionId;
  const selectedSession = sessions.find(
    (session) => session.native_session_id === currentSessionId,
  );
  const config =
    configOverride && configOverride.anchor === openedSession
      ? configOverride.config
      : selectedSession
        ? sessionConfig(selectedSession, baseMachine)
        : profileRunConfig(project.agent_profiles.paper_coach);
  const sessionTasks = conversation.tasks;
  const activeTask = sessionTasks.find(isActiveTask);
  const steeringTask =
    activeTask?.steer_visible && activeTask.can_steer && activeTask.steer_turn_id
      ? activeTask
      : null;
  const [receivedSteers, setReceivedSteers] = useState<ChatMessage[]>([]);
  const steers = useMemo(() => {
    const byId = new Map(
      sessionTasks.flatMap((task) => task.steers ?? []).map((steer) => [steer.message_id, steer]),
    );
    for (const steer of receivedSteers)
      if (sessionTasks.some((task) => task.operation_id === steer.operation_id))
        byId.set(steer.message_id, steer);
    return [...byId.values()];
  }, [sessionTasks, receivedSteers]);
  const lines = useAgentWindowLines({ messages: steers, tasks: sessionTasks });
  // The red edge and its icon carry failures and pauses.
  const transcript = useMemo(
    () =>
      coachSteersAfterPrompts(lines.transcript).filter(
        (line) => line.role !== "error" && line.role !== "meta",
      ),
    [lines.transcript],
  );
  const endpoints = useMemo(
    () => projectAgentWindowEndpoints(project.id, `paper-coach:${openedSession ?? ""}`),
    [project.id, openedSession],
  );
  const agentPicker = useAgentConfigPicker({
    project,
    value: config,
    onChange: (next) => setConfigOverride({ anchor: openedSession, config: next }),
    label: "Coach agent",
    disabled: Boolean(activeTask) || submitting,
    workLikeCapable: false,
    panelClassName: "paper-agent-popover",
  });
  const readiness = project.provider_readiness[config.run_on]?.[config.provider];
  const skills = useSkillPicker({
    catalog: project.skill_catalog ?? [],
    defaults: project.skill_defaults ?? EMPTY_SKILL_SELECTION,
    provider: config.provider,
    providerLabel: readiness?.label || config.provider,
    machine: config.run_on,
    inventory: project.provider_skill_inventories?.[config.run_on]?.[config.provider],
    getMessage: () => message,
    onComplete: (next) => {
      setMessage(next);
      setError(null);
      requestAnimationFrame(() => textarea.current?.focus());
    },
  });
  const steer = async (task: AgentTask) => {
    if (!text || composer.staged || submitting || !task.steer_turn_id) return;
    if (!comments.requireComplete()) return;
    const sent = comments.annotations;
    setSubmitting(true);
    setError(null);
    try {
      const receipt = await endpoints.steer(task.operation_id, {
        message_id: crypto.randomUUID(),
        attempt: task.attempt,
        expected_turn_id: task.steer_turn_id,
        message: text,
      });
      setReceivedSteers((current) => [
        ...current.filter((item) => item.message_id !== receipt.message_id),
        receipt,
      ]);
      setMessage("");
      skills.reset();
      comments.clear(sent);
    } catch (cause) {
      setError(`Steering receipt could not be read. Nothing was resent. ${String(cause)}`);
    } finally {
      setSubmitting(false);
    }
  };
  const send = async () => {
    if (steeringTask) return steer(steeringTask);
    if (!text || composer.unready || activeTask || submitting) return;
    if (!comments.requireComplete()) return;
    const sent = comments.annotations;
    composer.closeMenu();
    setSubmitting(true);
    setError(null);
    try {
      const task = await onStartTask("paper_coach", {
        message: text,
        ...config,
        model: config.model || null,
        session_id: currentSessionId,
        paper_apply_directly: applyDirectly,
        ...skillInvocationFields(skills.selection, skills.providerSkillNames),
        ...composer.turnFields(),
      });
      // The conversation follows this turn, even into the fresh session a provider
      // switch starts, and stays anchored where it opened.
      setSessionId(openedSession);
      setStarted((current) => [...current, task]);
      setMessage("");
      skills.reset();
      composer.clear();
      comments.clear(sent);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setSubmitting(false);
    }
  };
  // One UUID per attempt, so a repeated click replays the same retry admission.
  const retryKeys = useRef(new Map<string, string>());
  const retry = async (task: AgentTask) => {
    // A paused or interrupted turn continues in its own session; a failed one retries.
    const action = task.can_resume ? "resume" : "retry";
    setSubmitting(true);
    setError(null);
    const attempt = `${task.operation_id}:${task.attempt}:${action}`;
    const key = retryKeys.current.get(attempt) ?? crypto.randomUUID();
    retryKeys.current.set(attempt, key);
    try {
      const next = await api<AgentTask>(
        `${apiBase}/tasks/${encodeURIComponent(task.operation_id)}/${action}`,
        {
          method: "POST",
          headers: { "Idempotency-Key": key },
          ...(action === "retry"
            ? { body: JSON.stringify(taskRetryRequestBody(task, config)) }
            : {}),
        },
      );
      setStarted((current) => [...current, next]);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setSubmitting(false);
    }
  };
  const stop = async (task: AgentTask) => {
    setError(null);
    try {
      await api(`${apiBase}/tasks/${encodeURIComponent(task.operation_id)}/pause`, {
        method: "POST",
      });
    } catch (cause) {
      setError(String(cause));
    }
  };
  const retryButton = (task: AgentTask) => (
    <button
      className="icon-button compact paper-reply-retry"
      aria-label={task.can_resume ? "Resume coach reply" : "Retry coach reply"}
      title={task.error || undefined}
      disabled={submitting || !(task.can_retry || task.can_resume)}
      onClick={() => void retry(task)}
    >
      <RotateCcw size={15} />
    </button>
  );
  // A resumed or retried turn continues in a child task; only the newest attempt shows.
  const continued = new Set(sessionTasks.flatMap((task) => task.parent_operation_id ?? []));
  // Failed, stopped, or cut off by a restart: each shows its edge and a way forward.
  const stuck = (task: AgentTask | undefined): task is AgentTask =>
    !!task &&
    !continued.has(task.operation_id) &&
    !isActiveTask(task) &&
    (task.failed || task.can_resume || task.can_retry);
  const cards = (taskId: string) => (
    <PaperChanges
      apiBase={apiBase}
      changes={changes.filter((change) => change.task_id === taskId)}
      onAction={onChangeAction}
      onUndoAll={onUndoAll}
      onLocate={onLocate}
    />
  );
  // Each turn's cards, and its edge when stuck, follow its last reply.
  const lastReplies = new Map(
    transcript.filter((line) => line.role === "agent").map((line) => [line.taskId, line.lineId]),
  );
  const replyTask = (line: TaskTranscriptLine) =>
    lastReplies.get(line.taskId) === line.lineId
      ? sessionTasks.find((task) => task.operation_id === line.taskId)
      : undefined;
  const artifacts = useAgentWindowArtifacts({
    endpoints,
    chatId: endpoints.scopeKey,
    relatedTasks: sessionTasks,
    transcript,
    comments,
    onRefreshTask: endpoints.loadTask,
    setSubmitError: setError,
  });
  return (
    <div className="chat-dock paper-coach" data-mode="discuss">
      {panelTools(
        toolbar,
        <>
          <AgentStatusStrip tasks={sessionTasks} />
          <button
            ref={historyButton}
            className="icon-button compact"
            aria-label="Coach chat history"
            aria-expanded={historyOpen && sessions.length > 0}
            disabled={!sessions.length}
            onClick={() => setHistoryOpen(!historyOpen)}
          >
            <History size={15} />
          </button>
          <button
            className="icon-button compact"
            aria-label="New coach chat"
            onClick={() => {
              setSessionId(null);
              setFresh(true);
              setStarted([]);
              setConfigOverride(null);
              skills.reset();
              setError(null);
            }}
          >
            <MessageCirclePlus size={15} />
          </button>
          {sessionError && (
            <button
              className="icon-button compact"
              aria-label="Retry chat history"
              title={sessionError}
              onClick={() => setSessionRefresh((value) => value + 1)}
            >
              <RotateCcw size={15} />
            </button>
          )}
        </>,
        Boolean(sessionError),
      )}
      {historyOpen && sessions.length > 0 && (
        <div className="paper-session-list" ref={historyList}>
          {sessions.map((session) => (
            <button
              key={session.native_session_id}
              aria-pressed={currentSessionId === session.native_session_id}
              onClick={() => attach(session)}
            >
              {session.title || new Date(session.created_at).toLocaleString()}
            </button>
          ))}
        </div>
      )}
      <div
        className="node-chat-lines paper-coach-transcript"
        aria-live="polite"
        ref={transcriptRef}
      >
        <AgentWindowTranscript
          transcript={transcript}
          tasks={sessionTasks}
          questions={NO_QUESTIONS}
          endpoints={endpoints}
          artifacts={artifacts}
          comments={comments}
          compactionByLine={lines.compactionByLine}
          continuedTaskIds={lines.continuedTaskIds}
          latestGraphReceiptLineIds={lines.latestGraphReceiptLineIds}
          showModes={false}
          submitting={submitting}
          agentProgress={() => null}
          lineClassName={(line) =>
            stuck(replyTask(line)) ? `${line.role} paper-failure` : line.role
          }
          lineTrail={(line) => {
            const task = replyTask(line);
            return (
              task && (
                <>
                  {stuck(task) && retryButton(task)}
                  {cards(task.operation_id)}
                </>
              )
            );
          }}
          footer={
            <>
              {sessionTasks
                .filter((task) => !lastReplies.has(task.operation_id))
                .map((task) => (
                  <div
                    key={task.operation_id}
                    className={`node-chat-line agent${stuck(task) ? " paper-failure bare" : ""}`}
                  >
                    {stuck(task) && retryButton(task)}
                    {cards(task.operation_id)}
                  </div>
                ))}
              {!sessionTasks.length && !fresh && (
                <PaperChanges
                  apiBase={apiBase}
                  changes={changes}
                  onAction={onChangeAction}
                  onUndoAll={onUndoAll}
                  onLocate={onLocate}
                />
              )}
              {activeTask && <LoaderCircle size={16} className="spin" aria-label="Coach running" />}
            </>
          }
        />
      </div>
      <ChatComposer
        hint={
          <ProviderSwitchHint
            project={project}
            config={config}
            tasks={sessionTasks}
            sessionId={currentSessionId}
          />
        }
        mode="discuss"
        composer={composer}
        comments={comments}
        skills={skills.props}
        addDisabled={composer.full || composer.preparing || submitting}
        className={error ? "paper-failure" : undefined}
        field={
          <ComposerField
            ref={textarea}
            aria-label="Message"
            value={message}
            onChange={(event) => {
              setMessage(event.target.value);
              skills.readMessage(event.target.value);
              setError(null);
            }}
            onPaste={composer.paste}
            onKeyDown={(event) => {
              if (skills.handleKeyDown(event)) return;
              if (returnSends(event)) {
                event.preventDefault();
                void send();
              }
            }}
          />
        }
        tools={
          <>
            {agentPicker.chip}
            <div className="segmented" role="group" aria-label="Coach changes">
              {([false, true] as const).map((apply) => (
                <button
                  key={String(apply)}
                  aria-label={apply ? "Accept edits" : "Review edits"}
                  title={apply ? "Accept edits" : "Review edits"}
                  aria-pressed={applyDirectly === apply}
                  onClick={() => {
                    setApplyDirectly(apply);
                    writePref(modeKey, apply ? "apply" : "review");
                  }}
                >
                  {apply ? <Zap size={15} /> : <ListChecks size={15} />}
                </button>
              ))}
            </div>
          </>
        }
        actions={
          <>
            {activeTask?.can_pause && (
              <button
                className="icon-button"
                aria-label="Stop coach"
                onClick={() => void stop(activeTask)}
              >
                <Square size={14} />
              </button>
            )}
            <button
              className="icon-button primary chat-send-button"
              aria-label={
                steeringTask
                  ? steeringTask.steer_action_label
                  : error
                    ? "Retry message"
                    : "Send coach message"
              }
              title={
                (activeTask && !steeringTask ? activeTask.steer_unavailable_reason : error) ||
                undefined
              }
              disabled={
                !text ||
                !comments.complete ||
                submitting ||
                (steeringTask
                  ? composer.staged
                  : composer.unready ||
                    Boolean(activeTask) ||
                    !launchProviderReady(project, config, false, Boolean(currentSessionId)))
              }
              onClick={() => void send()}
            >
              {error && !steeringTask ? <RotateCcw size={16} /> : <Send size={16} />}
            </button>
          </>
        }
      />
      {agentPicker.panel}
    </div>
  );
}
