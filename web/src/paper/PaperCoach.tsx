import { useEffect, useMemo, useRef, useState } from "react";
import {
  History,
  ListChecks,
  LoaderCircle,
  MessageCirclePlus,
  RotateCcw,
  Send,
  X,
  Zap,
} from "lucide-react";
import { isActiveTask, reconstructTaskTranscript, relatedCoachTasks } from "../agents/agentTasks";
import {
  AgentConfigChip,
  AgentConfigControls,
  launchProviderReady,
  profileRunConfig,
} from "../core/AgentConfigControls";
import { api } from "../core/api";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { MarkdownAnswer } from "../core/chatMarkdown";
import { SkillPicker, useSkillPicker } from "../core/SkillPicker";
import { EMPTY_SKILL_SELECTION, skillInvocationFields } from "../core/skillPickerModel";
import type {
  AgentRunConfig,
  AgentTask,
  ProjectSnapshot,
  StartAgentTask,
  WritingSession,
} from "../core/types";
import { taskRetryRequestBody } from "../taskRecovery";
import { PaperChanges, type PaperChange, type PaperChangeAction } from "./PaperChanges";
import "./paper-coach.css";

function sessionConfig(session: WritingSession): AgentRunConfig {
  return {
    provider: session.provider,
    model: session.model === "provider-default" ? "" : session.model,
    reasoning: session.reasoning ?? "medium",
    run_on: session.execution_machine,
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
}: {
  apiBase: string;
  project: ProjectSnapshot;
  tasks: AgentTask[];
  onStartTask: StartAgentTask;
  changes: PaperChange[];
  onChangeAction: (groupId: string, action: PaperChangeAction) => Promise<void>;
  onUndoAll: (taskId: string) => Promise<void>;
  onLocate: (change: PaperChange) => void;
}) {
  const [sessions, setSessions] = useState<WritingSession[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [fresh, setFresh] = useState(false);
  const [pendingTask, setPendingTask] = useState<AgentTask | null>(null);
  const [config, setConfig] = useState<AgentRunConfig>(() =>
    profileRunConfig(project.agent_profiles.paper_coach),
  );
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [sessionRefresh, setSessionRefresh] = useState(0);
  const [configOpen, setConfigOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyButton = useRef<HTMLButtonElement>(null);
  const historyList = useRef<HTMLDivElement>(null);
  useLayerDismiss(
    historyOpen,
    () => setHistoryOpen(false),
    [historyList, historyButton],
    historyButton,
  );
  const [applyDirectly, setApplyDirectly] = useState(() => {
    try {
      return localStorage.getItem(`rcp:paper-coach-mode:${apiBase}`) === "apply";
    } catch {
      return false;
    }
  });
  const textarea = useRef<HTMLTextAreaElement>(null);
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
    setPendingTask(null);
    setConfig(sessionConfig(session));
    setHistoryOpen(false);
    setError(null);
  };
  const mergedTasks = useMemo(
    () =>
      pendingTask && !tasks.some((task) => task.operation_id === pendingTask.operation_id)
        ? [...tasks, pendingTask]
        : tasks,
    [tasks, pendingTask],
  );
  const currentSessionId =
    sessionId ??
    (!fresh
      ? (pendingTask?.native_session_id ??
        mergedTasks.find((task) => task.kind === "paper_coach" && task.native_session_id)
          ?.native_session_id ??
        sessions[0]?.native_session_id ??
        null)
      : null);
  const selectedSession = sessions.find(
    (session) => session.native_session_id === currentSessionId,
  );
  const configuredSession = useRef<string | null>(null);
  useEffect(() => {
    if (selectedSession && configuredSession.current !== selectedSession.native_session_id) {
      configuredSession.current = selectedSession.native_session_id;
      setConfig(sessionConfig(selectedSession));
    }
  }, [selectedSession]);
  const sessionTasks = currentSessionId
    ? relatedCoachTasks(mergedTasks, currentSessionId)
    : pendingTask
      ? [pendingTask]
      : [];
  const activeTask = sessionTasks.find(isActiveTask);
  const transcript = reconstructTaskTranscript(sessionTasks);
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
  const send = async () => {
    if (!message.trim() || activeTask || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const task = await onStartTask("paper_coach", {
        message: message.trim(),
        ...config,
        model: config.model || null,
        session_id: currentSessionId,
        paper_apply_directly: applyDirectly,
        ...skillInvocationFields(skills.selection, skills.providerSkillNames),
      });
      setPendingTask(task);
      setFresh(false);
      setMessage("");
      skills.reset();
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
      setPendingTask(next);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setSubmitting(false);
    }
  };
  const retryButton = (task: AgentTask) => (
    <button
      className="icon-button compact paper-reply-retry"
      aria-label="Retry coach reply"
      title={task.error || undefined}
      disabled={submitting || !(task.can_retry || task.can_resume)}
      onClick={() => void retry(task)}
    >
      <RotateCcw size={15} />
    </button>
  );
  // Failed, stopped, or cut off by a restart: each shows its edge and a way forward.
  const stuck = (task: AgentTask | undefined): task is AgentTask =>
    !!task && !isActiveTask(task) && (task.failed || task.can_resume || task.can_retry);
  const renderedTasks = new Set<string>();
  const cards = (taskId: string) => (
    <PaperChanges
      apiBase={apiBase}
      changes={changes.filter((change) => change.task_id === taskId)}
      onAction={onChangeAction}
      onUndoAll={onUndoAll}
      onLocate={onLocate}
    />
  );
  return (
    <div className="paper-coach">
      <div className={`paper-bar paper-coach-toolbar${sessionError ? " paper-failure" : ""}`}>
        <button
          ref={historyButton}
          className="icon-button compact"
          aria-label="Coach chat history"
          aria-expanded={historyOpen}
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
            setPendingTask(null);
            setConfig(profileRunConfig(project.agent_profiles.paper_coach));
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
      </div>
      {historyOpen && (
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
      <div className="paper-coach-transcript" aria-live="polite">
        {transcript
          .filter((line) => line.role !== "error")
          .map((line, index, lines) => {
            const task = sessionTasks.find((item) => item.operation_id === line.taskId);
            const lastAnswer =
              line.role === "agent" &&
              !lines
                .slice(index + 1)
                .some((next) => next.taskId === line.taskId && next.role === "agent");
            if (lastAnswer) renderedTasks.add(line.taskId);
            return (
              <div
                className={`paper-coach-message ${line.role}${lastAnswer && stuck(task) ? " paper-failure" : ""}`}
                key={line.lineId}
              >
                <MarkdownAnswer text={line.text} />
                {lastAnswer && stuck(task) && retryButton(task)}
                {lastAnswer && cards(line.taskId)}
              </div>
            );
          })}
        {sessionTasks
          .filter((task) => !renderedTasks.has(task.operation_id))
          .map((task) => (
            <div
              key={task.operation_id}
              className={`paper-coach-message agent${stuck(task) ? " paper-failure" : ""}`}
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
      </div>
      <div className={`paper-coach-composer${error ? " paper-failure" : ""}`}>
        <SkillPicker {...skills.props} />
        <textarea
          ref={textarea}
          aria-label="Message"
          value={message}
          onChange={(event) => {
            setMessage(event.target.value);
            skills.readMessage(event.target.value);
            setError(null);
          }}
          onKeyDown={(event) => {
            if (skills.handleKeyDown(event)) return;
            // A touch keyboard's Return writes a new line; the send button sends.
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !window.matchMedia("(pointer: coarse)").matches
            ) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <div className="paper-composer-actions">
          <AgentConfigChip
            project={project}
            value={config}
            label="Coach agent"
            open={configOpen}
            // A session keeps its agent; New chat picks another.
            disabled={Boolean(activeTask) || submitting || Boolean(currentSessionId)}
            onToggle={() => setConfigOpen(!configOpen)}
          />
          <div className="paper-segments" role="group" aria-label="Coach changes">
            {([false, true] as const).map((apply) => (
              <button
                key={String(apply)}
                aria-label={apply ? "Apply changes directly" : "Review changes"}
                title={apply ? "Apply changes directly" : "Review changes"}
                aria-pressed={applyDirectly === apply}
                onClick={() => {
                  setApplyDirectly(apply);
                  try {
                    localStorage.setItem(
                      `rcp:paper-coach-mode:${apiBase}`,
                      apply ? "apply" : "review",
                    );
                  } catch {}
                }}
              >
                {apply ? <Zap size={15} /> : <ListChecks size={15} />}
              </button>
            ))}
          </div>
          <button
            className="icon-button primary"
            aria-label={error ? "Retry message" : "Send coach message"}
            title={error || undefined}
            disabled={
              !message.trim() ||
              Boolean(activeTask) ||
              submitting ||
              !launchProviderReady(project, config)
            }
            onClick={() => void send()}
          >
            {error ? <RotateCcw size={16} /> : <Send size={16} />}
          </button>
        </div>
      </div>
      {configOpen && (
        <div className="paper-agent-popover">
          <button
            className="icon-button compact"
            aria-label="Close agent settings"
            onClick={() => setConfigOpen(false)}
          >
            <X size={14} />
          </button>
          <AgentConfigControls
            project={project}
            value={config}
            onChange={setConfig}
            workLikeCapable={false}
            compact
          />
        </div>
      )}
    </div>
  );
}
