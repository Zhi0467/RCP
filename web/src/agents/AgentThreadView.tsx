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
  Square,
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
const NO_QUESTIONS: AgentQuestion[] = [];
const NO_SCHEDULES: AgentSchedule[] = [];

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
/** A transcript as loaded for one thread; another thread's never renders here. */
type LoadedThread = {
  threadId: string;
  messages: ChatMessage[];
  questions: AgentQuestion[];
  latest: AgentTask | null;
};

/** One thread's composer: its draft and the send in flight or failed. */
type Composer = {
  draft: string;
  sending: { text: string; requestId: string } | null;
  error: string | null;
};

const EMPTY_COMPOSER: Composer = { draft: "", sending: null, error: null };

/** A message that belongs to one thread, shown only while that thread is open. */
type ThreadNote = { threadId: string; text: string };

export function AgentThreadView({
  thread,
  revision = null,
  leading,
  voice,
  onThreadChanged,
}: {
  thread: AgentThread;
  /** The member's agent-thread watermark; a change reloads this thread. */
  revision?: string | null;
  /** The rail's fold or the phone's back control, at the header's start. */
  leading?: ReactNode;
  voice: VoiceAgent | null;
  onThreadChanged: (thread: AgentThread) => void;
}) {
  const [loaded, setLoaded] = useState<LoadedThread | null>(null);
  const [loadError, setLoadError] = useState<ThreadNote | null>(null);
  const [notice, setNotice] = useState<ThreadNote | null>(null);
  const [scheduleList, setScheduleList] = useState<{
    threadId: string;
    schedules: AgentSchedule[];
  } | null>(null);
  const [turnTasks, setTurnTasks] = useState<ReadonlyMap<string, AgentTask>>(() => new Map());
  const [composers, setComposers] = useState<ReadonlyMap<string, Composer>>(() => new Map());
  const [chosen, setChosen] = useState<AgentRunConfig | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const linesRef = useRef<HTMLDivElement>(null);
  const followRef = useRef(true);
  const picker = useThreadPicker(thread);
  const threadRef = useRef(thread);
  threadRef.current = thread;
  const isCurrent = (threadId: string) => threadRef.current.thread_id === threadId;

  const view = loaded?.threadId === thread.thread_id ? loaded : null;
  const messages = view?.messages ?? null;
  const questions = view?.questions ?? NO_QUESTIONS;
  const latest = view?.latest ?? null;
  const schedules =
    scheduleList?.threadId === thread.thread_id ? scheduleList.schedules : NO_SCHEDULES;
  const shownLoadError = loadError?.threadId === thread.thread_id ? loadError.text : null;
  const shownNotice = notice?.threadId === thread.thread_id ? notice.text : null;
  const composer = composers.get(thread.thread_id) ?? EMPTY_COMPOSER;
  const updateComposer = useCallback((threadId: string, change: Partial<Composer>) => {
    setComposers((current) => {
      const next = new Map(current);
      next.set(threadId, { ...(current.get(threadId) ?? EMPTY_COMPOSER), ...change });
      return next;
    });
  }, []);

  // Every answer names the thread it was asked for; one for a thread no longer open is dropped.
  const refresh = useCallback(async () => {
    const current = threadRef.current;
    try {
      const [transcript, asked] = await Promise.all([
        loadThreadTranscript(current),
        loadThreadQuestions(current),
      ]);
      // A running project turn writes its member message only when it settles.
      const turnId = current.latest_operation_id ?? latestTurnId(transcript.messages);
      const latestTask = turnId ? await loadThreadTask(current, turnId) : null;
      if (threadRef.current.thread_id !== current.thread_id) return;
      setLoaded({
        threadId: current.thread_id,
        messages: transcript.messages,
        questions: asked,
        latest: latestTask,
      });
      setLoadError(null);
    } catch (failure) {
      if (threadRef.current.thread_id === current.thread_id)
        setLoadError({ threadId: current.thread_id, text: errorMessage(failure) });
    }
  }, []);

  const refreshSchedules = useCallback(async () => {
    const current = threadRef.current;
    try {
      const list = await loadThreadSchedules(current.thread_id);
      if (threadRef.current.thread_id === current.thread_id)
        setScheduleList({ threadId: current.thread_id, schedules: list.schedules });
    } catch {
      // Upcoming is a side list; the thread stays usable without it.
    }
  }, []);

  // Another thread starts with its own picks; the watermark reloads the open one.
  useEffect(() => {
    setChosen(null);
    setRenaming(false);
    followRef.current = true;
  }, [thread.thread_id]);
  useEffect(() => {
    void refresh();
    void refreshSchedules();
  }, [refresh, refreshSchedules, thread.thread_id, revision]);

  // While a turn runs, its progress and outcome are polled until it settles.
  const live = latest?.active ?? false;
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => void refresh(), LIVE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [live, refresh]);

  // Settled turns never change, so each is kept once fetched. A failed read changes
  // nothing here and is tried again when the transcript next loads.
  const latestId = latest?.operation_id ?? null;
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
      .filter((id) => !turnTasks.has(id) && id !== latestId);
    if (!wanted.length) return;
    const owner = threadRef.current;
    let current = true;
    void Promise.all(wanted.map((id) => loadThreadTask(owner, id).catch(() => null))).then(
      (tasks) => {
        if (!current) return;
        const settled = tasks.filter((task): task is AgentTask => Boolean(task?.settled));
        if (!settled.length) return;
        setTurnTasks((known) => {
          const next = new Map(known);
          for (const task of settled) next.set(task.operation_id, task);
          return next;
        });
      },
    );
    return () => {
      current = false;
    };
  }, [messages, turnTasks, latestId]);

  // Reading marks the open thread read through its own newest message, once it loaded.
  const loadedThreadId = view?.threadId ?? null;
  const newest = messages ? readThrough(messages) : null;
  useEffect(() => {
    if (!thread.unread || !newest || loadedThreadId !== thread.thread_id) return;
    markThreadRead(thread.thread_id, newest).then(onThreadChanged, () => undefined);
  }, [thread.unread, thread.thread_id, loadedThreadId, newest, onThreadChanged]);

  // The view follows new lines only while the reader sits at the bottom.
  useEffect(() => {
    const lines = linesRef.current;
    if (lines && followRef.current) lines.scrollTop = lines.scrollHeight;
  }, [messages, questions, composer.sending, latest]);

  const config = chosen ?? picker?.value ?? null;
  const agentPicker = useAgentConfigPicker({
    project: picker?.source ?? { provider_readiness: {}, machines: [] },
    value: config ?? { provider: "codex", model: "", reasoning: "", run_on: "" },
    onChange: setChosen,
    label: `${thread.agent_name} agent`,
    workLike: true,
    panelClassName: "chat-agent-config-panel",
  });

  // A send belongs to the thread it was typed in, wherever the member goes meanwhile.
  const send = async (text: string, requestId: string) => {
    const target = thread;
    updateComposer(target.thread_id, { sending: { text, requestId }, error: null });
    try {
      await sendThreadTurn(
        target,
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
      if (isCurrent(target.thread_id)) {
        followRef.current = true;
        // The sent line stays until the transcript that holds it arrives.
        await refresh();
      }
      setComposers((current) => {
        const entry = current.get(target.thread_id) ?? EMPTY_COMPOSER;
        const next = new Map(current);
        next.set(target.thread_id, {
          draft: entry.draft.trim() === text ? "" : entry.draft,
          sending: null,
          error: null,
        });
        return next;
      });
    } catch (failure) {
      updateComposer(target.thread_id, { error: errorMessage(failure) });
    }
  };
  const submit = () => {
    const text = composer.draft.trim();
    if (!text || (composer.sending && !composer.error)) return;
    void send(text, crypto.randomUUID());
  };

  const control = async (action: "pause" | "resume" | "retry", task: AgentTask) => {
    const target = thread;
    setControlBusy(true);
    try {
      await controlThreadTask(target, task.operation_id, action, crypto.randomUUID());
    } catch (failure) {
      if (isCurrent(target.thread_id))
        setNotice({ threadId: target.thread_id, text: errorMessage(failure) });
    } finally {
      setControlBusy(false);
      if (isCurrent(target.thread_id)) void refresh();
    }
  };
  const noteFailure = (threadId: string) => (failure: unknown) =>
    setNotice({ threadId, text: errorMessage(failure) });

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
  const pending = composer.sending;
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
          noteFailure(thread.thread_id),
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
                  void renameThread(thread.thread_id, title).then(
                    onThreadChanged,
                    noteFailure(thread.thread_id),
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
        {messages === null && !shownLoadError && (
          <div className="node-chat-line meta" role="status">
            <LoaderCircle className="spin" size={14} />
          </div>
        )}
        {shownLoadError && (
          <div className="node-chat-line error" role="alert">
            {shownLoadError}
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
        {!pending &&
          latest?.request.message &&
          !messages?.some((message) => message.operation_id === latest.operation_id) && (
            <div className="node-chat-line human">
              <div className="chat-human-message">
                <span className="node-chat-text">{latest.request.message}</span>
              </div>
            </div>
          )}
        {pending &&
          !messages?.some(
            (message) => message.text === pending.text && message.role === "user",
          ) && (
            <div className={`node-chat-line human${composer.error ? " unsent" : ""}`}>
              {composer.error && (
                <button
                  type="button"
                  className="icon-button chat-turn-resend"
                  aria-label="Resend"
                  title={composer.error}
                  onClick={() => void send(pending.text, pending.requestId)}
                >
                  <RotateCcw size={14} />
                </button>
              )}
              <div className="chat-human-message">
                <span className="node-chat-text">{pending.text}</span>
              </div>
            </div>
          )}
        {latest?.active && (
          <div className="node-chat-line agent">
            <TurnProgress task={latest} />
            <span className="agent-turn-live" role="status">
              <LoaderCircle className="spin" size={12} aria-hidden="true" />
              {latest.status_message}
              {latest.can_pause && (
                <button
                  type="button"
                  className="button compact secondary"
                  disabled={controlBusy || latest.pausing}
                  onClick={() => void control("pause", latest)}
                >
                  <Square size={11} /> Stop
                </button>
              )}
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
        {shownNotice && (
          <div className="node-chat-line error" role="alert">
            {shownNotice}
          </div>
        )}
      </div>
      <div className="chat-composer" data-mode="work">
        <textarea
          aria-label="Message"
          value={composer.draft}
          onChange={(event) => updateComposer(thread.thread_id, { draft: event.target.value })}
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
              disabled={!composer.draft.trim() || (pending !== null && !composer.error)}
              onClick={submit}
            >
              {pending && !composer.error ? (
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
