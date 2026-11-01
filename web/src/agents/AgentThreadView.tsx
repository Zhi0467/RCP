import {
  CalendarClock,
  ChevronRight,
  GitPullRequestArrow,
  LoaderCircle,
  Mic,
  MicOff,
  Play,
  RotateCcw,
  Send,
  X,
} from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAgentConfigPicker } from "../core/AgentConfigPicker";
import type { AgentConfigSource } from "../core/AgentConfigControls";
import { loadProjectReadiness, loadSpaceMachines } from "../core/api";
import { MarkdownAnswer } from "../core/chatMarkdown";
import { errorMessage } from "../core/errors";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { returnSends } from "../ui/composerKeys";
import { OverflowMenu } from "../ui/OverflowMenu";
import { AgentRename } from "../chat/AgentRename";
import { QuestionCard } from "../chat/QuestionCard";
import type {
  AgentQuestion,
  AgentRunConfig,
  AgentSchedule,
  AgentTask,
  AgentThread,
  ChatMessage,
  ProviderReadiness,
} from "../core/types";
import type { VoiceAgent } from "../voice/useVoiceAgent";
import { ApprovalCardView } from "./ApprovalCard";
import {
  browserTimezone,
  latestTurnId,
  readThrough,
  scheduleWhen,
  turnCommands,
  turnCommandsSummary,
  upcomingSchedules,
} from "./agentThreadModel";
import {
  cancelThreadSchedule,
  controlThreadTask,
  enableThreadSchedule,
  loadThreadQuestions,
  loadThreadSchedules,
  loadThreadTask,
  loadThreadTranscript,
  markThreadRead,
  renameThread,
  sendThreadTurn,
  setThreadBrowser,
} from "./agentThreadsApi";

/** A live turn is checked this often; a quiet thread follows its rail row instead. */
const LIVE_POLL_MS = 2_000;
/** Settled turns whose command lines are fetched, newest first. */
const COMMAND_TURNS = 12;

type Picker = { source: AgentConfigSource; value: AgentRunConfig } | null;

/** What the provider chip reads: the project's readiness, or the backend machine's. */
function useThreadPicker(thread: AgentThread): Picker {
  const [picker, setPicker] = useState<Picker>(null);
  useEffect(() => {
    let current = true;
    setPicker(null);
    const load = async (): Promise<Picker> => {
      if (thread.kind === "project" && thread.project_id) {
        const readiness = await loadProjectReadiness(
          `/api/projects/${encodeURIComponent(thread.project_id)}`,
        );
        const profile = readiness.agent_profiles.project_chat;
        return {
          source: { provider_readiness: readiness.provider_readiness, machines: [] },
          value: {
            provider: profile.provider,
            model: profile.model,
            reasoning: profile.reasoning,
            run_on: profile.run_on,
          },
        };
      }
      // The Space thread runs on the backend's own machine, the one with no host.
      const machine = (await loadSpaceMachines()).find((item) => !item.host);
      if (!machine) return null;
      const providers = Object.values(machine.provider_readiness) as ProviderReadiness[];
      const first = providers.find((item) => item.authenticated) ?? providers[0];
      if (!first) return null;
      return {
        source: {
          provider_readiness: {
            [machine.machine_id]: machine.provider_readiness as Record<string, ProviderReadiness>,
          },
          machines: [],
        },
        value: { provider: first.provider, model: "", reasoning: "", run_on: machine.machine_id },
      };
    };
    // Without readiness the chip stays away and the backend picks the provider.
    load()
      .then((loaded) => current && setPicker(loaded))
      .catch(() => current && setPicker(null));
    return () => {
      current = false;
    };
  }, [thread.kind, thread.project_id]);
  return picker;
}

function TurnProgress({ task }: { task: AgentTask }) {
  const commands = turnCommands(task);
  if (!commands.length) return null;
  return (
    <details className="chat-progress">
      <summary>
        <ChevronRight className="chev" size={12} aria-hidden="true" />
        {turnCommandsSummary(commands)}
      </summary>
      <ol>
        {commands.map((command) => (
          <li key={command.id}>
            {command.text}
            {command.seconds !== null && <span>{command.seconds}s</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}

function SchedulesPopover({
  thread,
  schedules,
  onChanged,
}: {
  thread: AgentThread;
  schedules: AgentSchedule[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useLayerDismiss(open, close, [trigger, panel], trigger);
  const upcoming = upcomingSchedules(schedules);
  if (!upcoming.length) return null;
  const act = async (run: () => Promise<unknown>) => {
    setError(null);
    try {
      await run();
    } catch (failure) {
      setError(errorMessage(failure));
    }
    onChanged();
  };
  return (
    <>
      <button
        ref={trigger}
        className="upcoming-button"
        type="button"
        title="Upcoming"
        aria-label={`${upcoming.length} upcoming`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <CalendarClock size={14} aria-hidden="true" />
        {upcoming.length}
      </button>
      {open && (
        <div className="upcoming-popover" role="dialog" aria-label="Upcoming" ref={panel}>
          <h3>Upcoming</h3>
          {upcoming.map((schedule) => (
            <div
              className="upcoming-row"
              data-state={schedule.state}
              key={schedule.schedule_id}
              title={
                schedule.disabled_reason === "restore"
                  ? "Paused by a restore"
                  : schedule.disabled_reason === "access_lost"
                    ? "Paused while you had no access to the project"
                    : undefined
              }
            >
              <strong>{schedule.note}</strong>
              <time>{scheduleWhen(schedule)}</time>
              {schedule.state === "active" ? (
                <button
                  className="icon-button"
                  type="button"
                  aria-label={`Cancel ${schedule.note}`}
                  title="Cancel"
                  onClick={() =>
                    void act(() => cancelThreadSchedule(thread.thread_id, schedule.schedule_id))
                  }
                >
                  <X size={14} />
                </button>
              ) : (
                <button
                  className="button compact"
                  type="button"
                  onClick={() =>
                    void act(() => enableThreadSchedule(thread.thread_id, schedule.schedule_id))
                  }
                >
                  Enable
                </button>
              )}
            </div>
          ))}
          {error && (
            <p className="upcoming-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </>
  );
}

/**
 * One of the member's agent threads: its transcript with progress, links and
 * approval cards, its Upcoming list, and one composer with the agent picker and
 * the mic. Every send is a Work turn as the member.
 */
export function AgentThreadView({
  thread,
  leading,
  voice,
  onThreadChanged,
}: {
  thread: AgentThread;
  /** The rail's fold or the phone's back control, at the header's start. */
  leading?: ReactNode;
  voice: VoiceAgent | null;
  onThreadChanged: (thread: AgentThread) => void;
}) {
  const [messages, setMessages] = useState<ChatMessage[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [questions, setQuestions] = useState<AgentQuestion[]>([]);
  const [schedules, setSchedules] = useState<AgentSchedule[]>([]);
  const [latest, setLatest] = useState<AgentTask | null>(null);
  const [turnTasks, setTurnTasks] = useState<Map<string, AgentTask>>(new Map());
  const [draft, setDraft] = useState("");
  const [chosen, setChosen] = useState<AgentRunConfig | null>(null);
  const [sending, setSending] = useState<{ text: string; requestId: string } | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const linesRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const picker = useThreadPicker(thread);
  const threadRef = useRef(thread);
  threadRef.current = thread;

  const refresh = useCallback(async () => {
    const current = threadRef.current;
    try {
      const [transcript, asked] = await Promise.all([
        loadThreadTranscript(current),
        loadThreadQuestions(current),
      ]);
      if (threadRef.current.thread_id !== current.thread_id) return;
      setMessages(transcript.messages);
      setQuestions(asked);
      setLoadError(null);
      const turnId = latestTurnId(transcript.messages);
      setLatest(turnId ? await loadThreadTask(current, turnId) : null);
    } catch (failure) {
      if (threadRef.current.thread_id === current.thread_id) setLoadError(errorMessage(failure));
    }
  }, []);

  const refreshSchedules = useCallback(async () => {
    const current = threadRef.current;
    try {
      const list = await loadThreadSchedules(current.thread_id);
      if (threadRef.current.thread_id === current.thread_id) setSchedules(list.schedules);
    } catch {
      // Upcoming is a side list; the thread stays usable without it.
    }
  }, []);

  // A new thread starts empty; the rail row's activity reloads the open one.
  useEffect(() => {
    setMessages(null);
    setQuestions([]);
    setSchedules([]);
    setLatest(null);
    setTurnTasks(new Map());
    setDraft("");
    setChosen(null);
    setSendError(null);
    setRenaming(false);
    followRef.current = true;
  }, [thread.thread_id]);
  useEffect(() => {
    void refresh();
    void refreshSchedules();
  }, [refresh, refreshSchedules, thread.thread_id, thread.last_activity_at, thread.live_state]);

  // While a turn runs, its progress and outcome are polled until it settles.
  const live = latest?.active ?? false;
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => void refresh(), LIVE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [live, refresh]);

  // Settled turns never change, so each one's commands are fetched once.
  useEffect(() => {
    if (!messages) return;
    const wanted = [
      ...new Set(
        messages
          .filter((message) => message.role === "assistant" && message.operation_id)
          .map((message) => message.operation_id!),
      ),
    ]
      .slice(-COMMAND_TURNS)
      .filter((id) => !turnTasks.has(id) && id !== latest?.operation_id);
    if (!wanted.length) return;
    let current = true;
    void Promise.all(wanted.map((id) => loadThreadTask(thread, id).catch(() => null))).then(
      (tasks) => {
        if (!current) return;
        setTurnTasks((known) => {
          const next = new Map(known);
          for (const task of tasks) if (task?.settled) next.set(task.operation_id, task);
          return next;
        });
      },
    );
    return () => {
      current = false;
    };
  }, [messages, thread, turnTasks, latest?.operation_id]);

  // Reading the thread marks it read through its newest message.
  const newest = messages ? readThrough(messages) : null;
  useEffect(() => {
    if (!thread.unread || !newest) return;
    markThreadRead(thread.thread_id, newest).then(onThreadChanged, () => undefined);
  }, [thread.unread, thread.thread_id, newest, onThreadChanged]);

  // The view follows new lines only while the reader sits at the bottom.
  useEffect(() => {
    const lines = linesRef.current;
    if (lines && followRef.current) lines.scrollTop = lines.scrollHeight;
  }, [messages, questions, sending, latest]);

  const config = chosen ?? picker?.value ?? null;
  const agentPicker = useAgentConfigPicker({
    project: picker?.source ?? { provider_readiness: {}, machines: [] },
    value: config ?? { provider: "codex", model: "", reasoning: "", run_on: "" },
    onChange: setChosen,
    label: `${thread.agent_name} agent`,
    workLike: true,
    panelClassName: "chat-agent-config-panel",
  });

  const send = async (text: string, requestId: string) => {
    setSending({ text, requestId });
    setSendError(null);
    try {
      await sendThreadTurn(
        thread,
        {
          message: text,
          // Only a pick made here changes the provider; otherwise the thread keeps its own.
          ...(chosen
            ? {
                provider: chosen.provider,
                ...(chosen.model ? { model: chosen.model } : {}),
                ...(chosen.reasoning ? { reasoning: chosen.reasoning } : {}),
              }
            : {}),
          timezone: browserTimezone(),
        },
        requestId,
      );
      setDraft("");
      followRef.current = true;
      await refresh();
      setSending(null);
    } catch (failure) {
      setSendError(errorMessage(failure));
    }
  };
  const submit = () => {
    const text = draft.trim();
    if (!text || (sending && !sendError)) return;
    void send(text, crypto.randomUUID());
  };

  const control = async (action: "pause" | "resume" | "retry", task: AgentTask) => {
    setControlBusy(true);
    try {
      await controlThreadTask(thread, task.operation_id, action, crypto.randomUUID());
    } catch (failure) {
      setSendError(errorMessage(failure));
    } finally {
      setControlBusy(false);
      void refresh();
    }
  };

  const questionsByTurn = useMemo(() => {
    const byTurn = new Map<string, AgentQuestion[]>();
    const loose: AgentQuestion[] = [];
    const turns = new Set(messages?.map((message) => message.operation_id) ?? []);
    for (const question of questions) {
      if (turns.has(question.operation_id)) {
        byTurn.set(question.operation_id, [...(byTurn.get(question.operation_id) ?? []), question]);
      } else loose.push(question);
    }
    return { byTurn, loose };
  }, [messages, questions]);

  const renderQuestion = (question: AgentQuestion) =>
    question.card ? (
      <ApprovalCardView
        key={question.question_id}
        question={question}
        card={question.card}
        projectName={thread.project_name}
        onResolved={() => void refresh()}
      />
    ) : thread.project_id ? (
      <QuestionCard
        key={question.question_id}
        question={question}
        apiBase={`/api/projects/${encodeURIComponent(thread.project_id)}`}
        onResolved={() => void refresh()}
      />
    ) : null;

  // A turn's questions follow its last line, so a card sits under the reply that asked.
  const lastLineOfTurn = new Map<string, number>();
  messages?.forEach((message, index) => {
    if (message.operation_id) lastLineOfTurn.set(message.operation_id, index);
  });

  const voiceHere = voice?.phase !== "idle" && voice?.thread?.thread_id === thread.thread_id;
  const voiceBusy = voice !== null && voice.phase !== "idle";
  const pausedTask = latest?.paused && latest.can_resume ? latest : null;
  const failedTask =
    latest && !latest.active && latest.can_retry && !latest.can_resume ? latest : null;

  const menuItems = [
    { label: "Rename", onSelect: () => setRenaming(true) },
    {
      label: thread.browser_requested ? "Turn browser off" : "Turn browser on",
      onSelect: () =>
        void setThreadBrowser(thread.thread_id, !thread.browser_requested).then(
          (result) => onThreadChanged({ ...thread, ...result }),
          (failure) => setSendError(errorMessage(failure)),
        ),
    },
  ];

  return (
    <div
      className="chat-dock workspace agent-thread"
      data-mode="work"
      role="region"
      aria-label={thread.agent_name}
    >
      <header className="conversation-header thread-header">
        {leading}
        <div className="conversation-heading">
          {renaming ? (
            <AgentRename
              title={thread.agent_name}
              onDone={(title) => {
                setRenaming(false);
                if (title && title !== thread.agent_name)
                  void renameThread(thread.thread_id, title).then(onThreadChanged, (failure) =>
                    setSendError(errorMessage(failure)),
                  );
              }}
            />
          ) : (
            <strong title={thread.project_name ?? undefined}>{thread.agent_name}</strong>
          )}
        </div>
        <div className="thread-header-tools">
          <SchedulesPopover
            thread={thread}
            schedules={schedules}
            onChanged={() => void refreshSchedules()}
          />
          <OverflowMenu
            label="Thread menu"
            items={menuItems}
            className="chat-header-menu"
            listClassName="chat-header-menu-list"
          />
        </div>
      </header>
      <div
        className="node-chat-lines"
        aria-live="polite"
        ref={linesRef}
        onScroll={(event) => {
          const element = event.currentTarget;
          followRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
        }}
      >
        {messages === null && !loadError && (
          <div className="node-chat-line meta" role="status">
            <LoaderCircle className="spin" size={14} />
          </div>
        )}
        {loadError && (
          <div className="node-chat-line error" role="alert">
            {loadError}
          </div>
        )}
        {messages?.map((message, index) => {
          const turn = message.operation_id ?? null;
          const task =
            turn && latest?.operation_id === turn ? latest : turn ? turnTasks.get(turn) : undefined;
          const closesTurn = turn !== null && lastLineOfTurn.get(turn) === index;
          return (
            <Fragment key={message.message_id}>
              {message.role === "user" ? (
                message.trigger === "schedule" ? (
                  <div className="node-chat-line agent">
                    <span className="chat-turn-trigger schedule" title={message.text}>
                      <CalendarClock size={11} aria-hidden="true" />
                      {new Date(message.timestamp).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                        hourCycle: "h23",
                      })}
                    </span>
                  </div>
                ) : (
                  <div className="node-chat-line human" data-source={message.source ?? undefined}>
                    <div className="chat-human-message">
                      {message.source === "voice" && (
                        <Mic className="chat-message-source" size={12} aria-label="Said by voice" />
                      )}
                      {message.source === "handoff" && (
                        <GitPullRequestArrow
                          className="chat-message-source"
                          size={12}
                          aria-label="Handed over from your Space thread"
                        />
                      )}
                      <span className="node-chat-text">{message.text}</span>
                    </div>
                  </div>
                )
              ) : (
                <div className="node-chat-line agent" data-source={message.source ?? undefined}>
                  {task && message.source !== "voice" && <TurnProgress task={task} />}
                  <div className="chat-markdown">
                    <MarkdownAnswer text={message.text} />
                  </div>
                </div>
              )}
              {closesTurn && questionsByTurn.byTurn.get(turn)?.map(renderQuestion)}
            </Fragment>
          );
        })}
        {questionsByTurn.loose.map(renderQuestion)}
        {sending &&
          !messages?.some(
            (message) => message.text === sending.text && message.role === "user",
          ) && (
            <div className={`node-chat-line human${sendError ? " unsent" : ""}`}>
              {sendError && (
                <button
                  type="button"
                  className="icon-button chat-turn-resend"
                  aria-label="Resend"
                  title={sendError}
                  onClick={() => void send(sending.text, sending.requestId)}
                >
                  <RotateCcw size={14} />
                </button>
              )}
              <div className="chat-human-message">
                <span className="node-chat-text">{sending.text}</span>
              </div>
            </div>
          )}
        {latest?.active && (
          <div className="node-chat-line agent">
            <TurnProgress task={latest} />
            <span className="chat-task-live" role="status">
              <LoaderCircle className="spin" size={12} aria-hidden="true" />
              {latest.status_message}
            </span>
          </div>
        )}
        {(pausedTask || failedTask) && (
          <div className="chat-task-inline paused" role="status">
            <span>{(pausedTask ?? failedTask)!.status_message}</span>
            {pausedTask && (
              <button
                type="button"
                className="button compact primary"
                disabled={controlBusy}
                onClick={() => void control("resume", pausedTask)}
              >
                <Play size={12} /> Resume
              </button>
            )}
            <button
              type="button"
              className="button compact secondary"
              disabled={controlBusy}
              onClick={() => void control("retry", (pausedTask ?? failedTask)!)}
            >
              <RotateCcw size={12} /> Retry
            </button>
          </div>
        )}
        {sendError && !sending && (
          <div className="node-chat-line error" role="alert">
            {sendError}
          </div>
        )}
      </div>
      <div className="chat-composer" data-mode="work">
        <textarea
          aria-label="Message"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (!returnSends(event) || event.nativeEvent.isComposing) return;
            event.preventDefault();
            submit();
          }}
        />
        <div className="chat-send">
          <div className="chat-composer-tools">{picker && config && agentPicker.chip}</div>
          <div className="chat-send-actions">
            {voice && (
              <button
                className={`icon-button chat-voice-button${voiceHere ? " active" : ""}`}
                type="button"
                aria-label={voiceBusy ? "End voice" : "Talk"}
                aria-pressed={voiceHere}
                title={
                  voiceBusy && !voiceHere
                    ? `End voice in ${voice.thread?.agent_name ?? "another thread"}`
                    : voiceBusy
                      ? "End voice"
                      : "Talk"
                }
                onClick={() => (voiceBusy ? voice.end() : void voice.open(thread))}
              >
                {voice.phase === "starting" && voiceHere ? (
                  <LoaderCircle className="spin" size={16} />
                ) : voiceBusy ? (
                  <MicOff size={16} />
                ) : (
                  <Mic size={16} />
                )}
              </button>
            )}
            <button
              className="icon-button primary chat-send-button"
              type="button"
              aria-label="Send"
              disabled={!draft.trim() || (sending !== null && !sendError)}
              onClick={submit}
            >
              {sending && !sendError ? (
                <LoaderCircle className="spin" size={16} />
              ) : (
                <Send size={16} />
              )}
            </button>
          </div>
        </div>
        {agentPicker.panel}
      </div>
    </div>
  );
}
