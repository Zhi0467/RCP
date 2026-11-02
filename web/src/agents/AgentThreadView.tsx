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
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AgentConfigChip,
  AgentConfigControls,
  type AgentConfigSource,
} from "../core/AgentConfigControls";
import { loadProjectReadiness, loadSpaceMachines } from "../core/api";
import { memberDraftKey } from "../core/draftStorage";
import { readPref, writePref } from "../core/prefStorage";
import { useResource } from "../core/useResource";
import { graphProfile } from "../graph/graphResources";
import { providerChange } from "../projects/providers";
import { ChatComposer } from "../chat/ChatComposer";
import { createComposerDraft, useComposerDraft, type ComposerDraft } from "../chat/composerDraft";
import { useComposerAttachments } from "../chat/composerAttachments";
import {
  assembleChatTurn,
  stagedArtifactContext,
  type StagedChatAnnotation,
} from "../chat/chatInput";
import { AnswerCommentLayers, transcriptSource, useAnswerComments } from "../chat/answerComments";
import { AgentStatusStrip } from "../chat/AgentStatusStrip";
import { AgentWindowTranscript } from "../agentWindow/AgentWindowTranscript";
import {
  useAgentWindowLines,
  type PendingAgentWindowTurn,
} from "../agentWindow/useAgentWindowLines";
import { useAgentWindowScroll } from "../agentWindow/useAgentWindowScroll";
import { useAgentWindowArtifacts } from "../agentWindow/useAgentWindowArtifacts";
import {
  projectAgentWindowEndpoints,
  spaceAgentWindowEndpoints,
} from "../agentWindow/agentWindowEndpoints";
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
  AgentThreadTurnRequest,
  ProjectSnapshot,
  SteerRequest,
  ProviderReadiness,
} from "../core/types";
import type { VoiceAgent } from "../voice/useVoiceAgent";
import { ApprovalCardView } from "./ApprovalCard";
import {
  browserTimezone,
  latestTurnId,
  nextTurnConfig,
  readThrough,
  scheduleWhen,
  turnCommands,
  turnCommandsSummary,
  upcomingSchedules,
} from "./agentThreadModel";
import {
  cancelThreadSchedule,
  enableThreadSchedule,
  loadThreadQuestions,
  loadThreadSchedules,
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
const NO_MESSAGES: ChatMessage[] = [];

/** Load settled turn details in bounded batches, newest first. */
const TURN_BATCH_SIZE = 12;

type Picker = { source: AgentConfigSource; value: AgentRunConfig } | null;

/**
 * What the agent pick reads, the project's readiness or the backend machine's,
 * and its default: the project's Settings profile, or the Space machine's first
 * signed-in provider with that provider's first model.
 */
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
      const { model, reasoning = "" } = providerChange(first.models ?? [], first.provider, "");
      return {
        source: {
          provider_readiness: {
            [machine.machine_id]: machine.provider_readiness as Record<string, ProviderReadiness>,
          },
          machines: [],
        },
        value: { provider: first.provider, model, reasoning, run_on: machine.machine_id },
      };
    };
    // Without readiness the pick stays away and the backend picks the provider.
    load()
      .then((loaded) => current && setPicker(loaded))
      .catch(() => current && setPicker(null));
    return () => {
      current = false;
    };
  }, [thread.kind, thread.project_id]);
  return picker;
}

/**
 * The thread's agent pick, one row of its menu: `provider · model · effort`,
 * which opens the shared fields beneath it. Space and project threads alike.
 */
function ThreadAgentPicker({
  source,
  value,
  label,
  onChange,
}: {
  source: AgentConfigSource;
  value: AgentRunConfig;
  label: string;
  onChange: (value: AgentRunConfig) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="thread-agent-picker">
      <AgentConfigChip
        project={source}
        value={value}
        open={open}
        label={label}
        workLike
        onToggle={() => setOpen(!open)}
      />
      {open && <AgentConfigControls project={source} value={value} onChange={onChange} compact />}
    </div>
  );
}

export function FreshSessionMarker({
  task,
  hasEarlierTurn,
}: {
  task: AgentTask | undefined;
  hasEarlierTurn: boolean;
}) {
  const resolution = task?.chat_session_resolution;
  if (!hasEarlierTurn || resolution?.outcome !== "fresh") return null;
  return (
    <span
      className="chat-turn-trigger fresh-session"
      title={
        resolution.reason_code === "provider_switch_compacted"
          ? "The provider changed. Earlier conversation was compacted for the new session."
          : resolution.reason_code === "provider_switch_tail"
            ? "The provider changed. Recent conversation was carried into the new session."
            : resolution.reason_code?.replaceAll("_", " ")
      }
    >
      <RotateCcw size={11} aria-hidden="true" />
      Fresh start
    </span>
  );
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
 * approval cards, its Upcoming list, its menu with the agent pick, and one
 * composer with the mic. Every send is a Work turn as the member.
 */
/** A transcript as loaded for one thread; another thread's never renders here. */
type LoadedThread = {
  threadId: string;
  messages: ChatMessage[];
  questions: AgentQuestion[];
  latest: AgentTask | null;
};

type PendingThreadTurn = PendingAgentWindowTurn & {
  requestId: string;
  draft: string;
  request: AgentThreadTurnRequest;
  annotations: StagedChatAnnotation[];
  clearAttachments: () => void;
  steering?: { taskId: string; request: SteerRequest };
};

/** The shared draft survives a thread switch along with its pending request. */
type Composer = { draft: ComposerDraft; sending: PendingThreadTurn | null; error: string | null };

/** A message that belongs to one thread, shown only while that thread is open. */
type ThreadNote = { threadId: string; text: string };

export function AgentThreadView({
  thread,
  actorId = null,
  project,
  revision = null,
  leading,
  voice,
  onThreadChanged,
}: {
  thread: AgentThread;
  actorId?: string | null;
  project?: ProjectSnapshot;
  /** The member's agent-thread watermark; a change reloads this thread. */
  revision?: string | null;
  /** The rail's fold or the phone's back control, at the header's start. */
  leading?: ReactNode;
  voice: VoiceAgent | null;
  onThreadChanged: (thread: AgentThread) => void;
}) {
  // Each thread's last view stays, so a switch shows it at once while it reloads.
  const [loaded, setLoaded] = useState<ReadonlyMap<string, LoadedThread>>(() => new Map());
  const [freshThreadId, setFreshThreadId] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<ThreadNote | null>(null);
  const [notice, setNotice] = useState<ThreadNote | null>(null);
  const [scheduleLists, setScheduleLists] = useState<ReadonlyMap<string, AgentSchedule[]>>(
    () => new Map(),
  );
  const [turnTasks, setTurnTasks] = useState<ReadonlyMap<string, AgentTask>>(() => new Map());
  const composers = useRef(new Map<string, Composer>());
  const [, refreshComposer] = useState(0);
  const [chosen, setChosen] = useState<AgentRunConfig | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const linesRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const picker = useThreadPicker(thread);
  const threadRef = useRef(thread);
  threadRef.current = thread;
  const isCurrent = (threadId: string) => threadRef.current.thread_id === threadId;

  const view = loaded.get(thread.thread_id) ?? null;
  const messages = view?.messages ?? null;
  const questions = view?.questions ?? NO_QUESTIONS;
  const latest = view?.latest ?? null;
  const schedules = scheduleLists.get(thread.thread_id) ?? NO_SCHEDULES;
  const shownLoadError = loadError?.threadId === thread.thread_id ? loadError.text : null;
  const shownNotice = notice?.threadId === thread.thread_id ? notice.text : null;
  const endpoints = useMemo(
    () =>
      thread.project_id
        ? projectAgentWindowEndpoints(thread.project_id, thread.thread_id)
        : spaceAgentWindowEndpoints(thread),
    [thread],
  );
  const endpointsRef = useRef(endpoints);
  endpointsRef.current = endpoints;
  const draftKey = memberDraftKey(actorId, `agent-window:${endpoints.scopeKey}`);
  const composerKey = `${actorId ?? ""}:${endpoints.scopeKey}`;
  if (!composers.current.has(composerKey))
    composers.current.set(composerKey, {
      draft: createComposerDraft(draftKey ? (readPref(draftKey) ?? "") : ""),
      sending: null,
      error: null,
    });
  const composer = composers.current.get(composerKey)!;
  const draft = composer.draft;
  const updateComposer = useCallback((key: string, change: Partial<Composer>) => {
    const current = composers.current.get(key);
    if (current) Object.assign(current, change);
    refreshComposer((value) => value + 1);
  }, []);
  useEffect(() => {
    if (!draftKey) return;
    const persist = () => writePref(draftKey, draft.get() || null);
    persist();
    return draft.subscribe(persist);
  }, [draft, draftKey]);
  const setSubmitError = (text: string | null) => updateComposer(composerKey, { error: text });
  const comments = useAnswerComments({
    storageKey: memberDraftKey(actorId, `agent-window-comments:${endpoints.scopeKey}`),
    enabled: true,
    busy: Boolean(composer.sending && !composer.error),
    sources: [transcriptSource(linesRef)],
    message: textareaRef,
    onError: setSubmitError,
  });
  const graph = useResource(
    `agent-window:${thread.thread_id}:references`,
    thread.project_id && !project ? graphProfile(thread.project_id) : null,
  );
  const snapshot = project ?? graph.value;
  const attachments = useComposerAttachments({
    project: thread.project_id ? (snapshot ?? { id: thread.project_id }) : null,
    attachmentsUrl: endpoints.attachmentsUrl,
    scopeId: thread.thread_id,
    referencesKey: memberDraftKey(actorId, `agent-window-references:${endpoints.scopeKey}`),
    input: textareaRef,
    message: draft,
    filesDisabled: Boolean(composer.sending && !composer.error),
    onError: setSubmitError,
  });
  const tasks = useMemo(() => {
    const ids = new Set(messages?.map((message) => message.operation_id) ?? []);
    const result = [...ids].flatMap((id) => {
      const task = id && id !== latest?.operation_id ? turnTasks.get(id) : undefined;
      return task ? [task] : [];
    });
    if (latest) result.push(latest);
    return result;
  }, [latest, messages, turnTasks]);
  const lines = useAgentWindowLines({
    messages: messages ?? NO_MESSAGES,
    tasks,
    pendingTurn: composer.sending,
  });
  const { shouldStickToBottomRef: followRef, handleChatScroll } = useAgentWindowScroll(
    endpoints.scopeKey,
    linesRef,
    lines.transcript,
    questions,
  );
  const refreshTask = async (taskId: string) => {
    const task = await endpoints.loadTask(taskId);
    setTurnTasks((current) => new Map(current).set(taskId, task));
    return task;
  };
  const artifacts = useAgentWindowArtifacts({
    endpoints,
    chatId: thread.thread_id,
    relatedTasks: tasks,
    transcript: lines.transcript,
    comments,
    onRefreshTask: refreshTask,
    setSubmitError,
    repositories: snapshot?.repositories ?? [],
    inlineArtifactRefreshToken: lines.inlineArtifactRefreshToken,
  });
  const artifactContext = thread.project_id ? stagedArtifactContext(comments.annotations) : null;
  const draftSendable = useComposerDraft(draft, (text) =>
    Boolean(assembleChatTurn(text, comments.annotations)),
  );

  // Every answer names the thread it was asked for; one for a thread no longer open is dropped,
  // and so is one older than a reload already shown.
  const refreshSeq = useRef({ started: 0, shown: 0 });
  // A send's own turn leads until the thread row reports a newer latest turn.
  const sentTurn = useRef<{ threadId: string; operationId: string; after: string | null } | null>(
    null,
  );
  const refresh = useCallback(async () => {
    const current = threadRef.current;
    const owner = endpointsRef.current;
    const seq = ++refreshSeq.current.started;
    const fresh = () =>
      threadRef.current.thread_id === current.thread_id && seq > refreshSeq.current.shown;
    try {
      const [transcript, asked] = await Promise.all([
        loadThreadTranscript(current),
        loadThreadQuestions(current),
      ]);
      const sent = sentTurn.current;
      // A running project turn writes its member message only when it settles.
      const turnId =
        sent?.threadId === current.thread_id && sent.after === current.latest_operation_id
          ? sent.operationId
          : (current.latest_operation_id ?? latestTurnId(transcript.messages));
      const latestTask = turnId ? await owner.loadTask(turnId) : null;
      if (!fresh()) return;
      refreshSeq.current.shown = seq;
      setLoaded((known) =>
        new Map(known).set(current.thread_id, {
          threadId: current.thread_id,
          messages: transcript.messages,
          questions: asked,
          latest: latestTask,
        }),
      );
      setFreshThreadId(current.thread_id);
      setLoadError(null);
    } catch (failure) {
      if (fresh()) setLoadError({ threadId: current.thread_id, text: errorMessage(failure) });
    }
  }, []);

  const refreshSchedules = useCallback(async () => {
    const current = threadRef.current;
    try {
      const list = await loadThreadSchedules(current.thread_id);
      if (threadRef.current.thread_id === current.thread_id)
        setScheduleLists((known) => new Map(known).set(current.thread_id, list.schedules));
    } catch {
      // Upcoming is a side list; the thread stays usable without it.
    }
  }, []);

  // Another thread starts with its own picks; the watermark reloads the open one.
  useEffect(() => {
    setChosen(null);
    setRenaming(false);
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
        messages.filter((message) => message.operation_id).map((message) => message.operation_id!),
      ),
    ]
      .filter((id) => !turnTasks.has(id) && id !== latestId)
      .slice(-TURN_BATCH_SIZE);
    if (!wanted.length) return;
    const owner = endpointsRef.current;
    let current = true;
    void Promise.all(wanted.map((id) => owner.loadTask(id).catch(() => null))).then((tasks) => {
      if (!current) return;
      const settled = tasks.filter((task): task is AgentTask => Boolean(task?.settled));
      if (!settled.length) return;
      setTurnTasks((known) => {
        const next = new Map(known);
        for (const task of settled) next.set(task.operation_id, task);
        return next;
      });
    });
    return () => {
      current = false;
    };
  }, [messages, turnTasks, latestId]);

  // Reading marks the open thread read through its own newest message, once it reloaded.
  const newest = messages ? readThrough(messages) : null;
  useEffect(() => {
    if (!thread.unread || !newest || freshThreadId !== thread.thread_id) return;
    markThreadRead(thread.thread_id, newest).then(onThreadChanged, () => undefined);
  }, [thread.unread, thread.thread_id, freshThreadId, newest, onThreadChanged]);

  // What the pick shows is exactly what the next send names.
  const config = chosen ?? (picker ? nextTurnConfig(messages, picker.value) : null);

  // A resend repeats the captured request, including its attachment set and provider.
  const send = async (pending: PendingThreadTurn) => {
    const target = thread;
    const key = composerKey;
    const sentComments = pending.annotations;
    updateComposer(key, { sending: pending, error: null });
    try {
      if (pending.steering) {
        await endpoints.steer(pending.steering.taskId, pending.steering.request);
      } else {
        const { operation_id } = await sendThreadTurn(target, pending.request, pending.requestId);
        sentTurn.current = {
          threadId: target.thread_id,
          operationId: operation_id,
          after: target.latest_operation_id,
        };
      }
      pending.clearAttachments();
      comments.discard(sentComments);
      if (isCurrent(target.thread_id)) {
        followRef.current = true;
        await refresh();
      }
      if (draft.get() === pending.draft) {
        draft.set("");
        if (draftKey) writePref(draftKey, null);
      }
      updateComposer(key, {
        sending: null,
        error: null,
      });
    } catch (failure) {
      const error = errorMessage(failure);
      updateComposer(key, { sending: { ...pending, error }, error });
    }
  };
  const steeringTask =
    latest?.active && latest.can_steer && latest.steer_visible && latest.steer_turn_id
      ? latest
      : null;
  const sendBlocked = Boolean(
    latest?.active && (!steeringTask || attachments.staged || artifactContext),
  );
  // A disabled Send says why on hover.
  const sendReason =
    composer.sending && !composer.error
      ? "Sending"
      : sendBlocked
        ? steeringTask
          ? "Attachments and comments wait until this turn ends"
          : "Wait until this turn ends"
        : attachments.unready
          ? "Attachments are still uploading"
          : !comments.complete
            ? "Finish the comment first"
            : !(draftSendable || artifactContext)
              ? "Write a message"
              : undefined;
  const submit = () => {
    const text = assembleChatTurn(draft.get(), comments.annotations);
    if (
      sendBlocked ||
      !(text || artifactContext) ||
      attachments.unready ||
      (composer.sending && !composer.error) ||
      !comments.requireComplete()
    )
      return;
    const fields = attachments.turnFields();
    const requestId = crypto.randomUUID();
    void send({
      clientId: requestId,
      requestId,
      text,
      draft: draft.get(),
      timestamp: new Date().toISOString(),
      mode: "work",
      attachments: attachments.ready,
      annotations: comments.annotations,
      clearAttachments: attachments.clear,
      ...(steeringTask
        ? {
            steering: {
              taskId: steeringTask.operation_id,
              request: {
                message_id: requestId,
                attempt: steeringTask.attempt,
                expected_turn_id: steeringTask.steer_turn_id!,
                message: text,
              },
            },
          }
        : {}),
      request: {
        message: text,
        ...(thread.project_id
          ? fields
          : {
              ...(fields.attachment_set_id
                ? {
                    attachment_set_id: fields.attachment_set_id,
                    attachment_client_id: fields.attachment_client_id,
                  }
                : {}),
            }),
        ...(artifactContext ? { artifact_context: artifactContext } : {}),
        ...(config
          ? {
              provider: config.provider,
              ...(config.model ? { model: config.model } : {}),
              ...(config.reasoning ? { reasoning: config.reasoning } : {}),
            }
          : {}),
        timezone: browserTimezone(),
      },
    });
  };

  const control = async (action: "pause" | "resume" | "retry", task: AgentTask) => {
    if (controlBusy) return;
    const target = thread;
    setNotice(null);
    setControlBusy(true);
    try {
      await endpoints.control(task.operation_id, action, crypto.randomUUID());
    } catch (failure) {
      if (isCurrent(target.thread_id))
        setNotice({ threadId: target.thread_id, text: errorMessage(failure) });
    } finally {
      setControlBusy(false);
      if (isCurrent(target.thread_id)) void refresh();
    }
  };
  // Each menu action replaces the last notice: it clears as the action starts.
  const act = (threadId: string, run: () => Promise<unknown>) => {
    setNotice(null);
    run().catch((failure: unknown) => setNotice({ threadId, text: errorMessage(failure) }));
  };

  const renderQuestion = (question: AgentQuestion) =>
    question.card ? (
      <ApprovalCardView
        key={question.question_id}
        question={question}
        card={question.card}
        projectName={thread.project_name}
        onResolved={() => void refresh()}
      />
    ) : endpoints.questionsApiBase ? (
      <QuestionCard
        key={question.question_id}
        question={question}
        apiBase={endpoints.questionsApiBase}
        onResolved={() => void refresh()}
      />
    ) : null;

  const turnIds = new Set([
    ...tasks.map((task) => task.operation_id),
    ...(messages ?? []).flatMap((message) => (message.operation_id ? [message.operation_id] : [])),
  ]);
  const firstLineOfTurn = new Map<string, string>();
  for (const line of lines.transcript) {
    if (turnIds.has(line.taskId) && !firstLineOfTurn.has(line.taskId))
      firstLineOfTurn.set(line.taskId, line.lineId);
  }
  const firstTurnId = firstLineOfTurn.keys().next().value;

  const voiceHere = voice?.phase !== "idle" && voice?.thread?.thread_id === thread.thread_id;
  const voiceBusy = voice !== null && voice.phase !== "idle";
  const pending = composer.sending;
  const pausedTask = latest?.paused && latest.can_resume ? latest : null;
  const failedTask =
    latest && !latest.active && latest.can_retry && !latest.can_resume ? latest : null;
  // A failed turn's error row carries its Retry; a stop with no error row shows it here.
  const stoppedTask =
    failedTask &&
    !lines.transcript.some(
      (line) => line.role === "error" && line.taskId === failedTask.operation_id,
    )
      ? failedTask
      : null;

  const menuItems = [
    { label: "Rename", onSelect: () => setRenaming(true) },
    {
      label: thread.browser_requested ? "Turn browser off" : "Turn browser on",
      onSelect: () =>
        act(thread.thread_id, () =>
          setThreadBrowser(thread.thread_id, !thread.browser_requested).then((result) =>
            onThreadChanged({ ...thread, ...result }),
          ),
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
                  act(thread.thread_id, () =>
                    renameThread(thread.thread_id, title).then(onThreadChanged),
                  );
              }}
            />
          ) : (
            <strong title={thread.project_name ?? undefined}>{thread.agent_name}</strong>
          )}
        </div>
        <div className="thread-header-tools">
          <AgentStatusStrip tasks={lines.statusTasks} />
          <SchedulesPopover
            thread={thread}
            schedules={schedules}
            onChanged={() => void refreshSchedules()}
          />
          <OverflowMenu
            label="Thread menu"
            items={menuItems}
            className="chat-header-menu"
            listClassName="chat-header-menu-list thread-menu-list"
          >
            {picker && config && (
              <ThreadAgentPicker
                source={picker.source}
                value={config}
                label={`${thread.agent_name} agent`}
                onChange={setChosen}
              />
            )}
          </OverflowMenu>
        </div>
      </header>
      <div
        className="node-chat-lines"
        aria-live="polite"
        ref={linesRef}
        onScroll={handleChatScroll}
      >
        {messages === null && !shownLoadError && (
          <div className="node-chat-line meta" role="status">
            <LoaderCircle className="spin" size={14} />
          </div>
        )}
        {shownLoadError && (
          <div className="node-chat-line error" role="alert">
            {shownLoadError}
            <button
              type="button"
              className="icon-button chat-error-retry"
              aria-label="Retry"
              title="Retry"
              onClick={() => void refresh()}
            >
              <RotateCcw size={14} />
            </button>
          </div>
        )}
        <AgentWindowTranscript
          transcript={lines.transcript}
          tasks={tasks}
          questions={questions}
          renderQuestion={renderQuestion}
          artifacts={artifacts}
          comments={comments}
          endpoints={endpoints}
          compactionByLine={lines.compactionByLine}
          continuedTaskIds={lines.continuedTaskIds}
          latestGraphReceiptLineIds={lines.latestGraphReceiptLineIds}
          showModes={false}
          lineClassName={(line) =>
            line.trigger === "schedule" && line.role === "human" ? "agent" : line.role
          }
          humanBody={(line) => (line.trigger === "schedule" ? <></> : undefined)}
          pendingTurn={pending}
          onRetryTask={(task) => void control("retry", task)}
          submitting={Boolean(pending && !composer.error)}
          onResend={() => pending && void send(pending)}
          nodes={snapshot?.graph?.nodes}
          lineLead={(line) => (
            <>
              {firstLineOfTurn.get(line.taskId) === line.lineId && (
                <FreshSessionMarker
                  task={tasks.find((task) => task.operation_id === line.taskId)}
                  hasEarlierTurn={line.taskId !== firstTurnId}
                />
              )}
              {line.trigger === "schedule" && line.role === "human" && (
                <span className="chat-turn-trigger schedule" title={line.text}>
                  <CalendarClock size={11} aria-hidden="true" />
                  {new Date(line.timestamp).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                    hourCycle: "h23",
                  })}
                </span>
              )}
            </>
          )}
          humanLead={(line) => (
            <>
              {line.source === "voice" && (
                <Mic className="chat-message-source" size={12} aria-label="Said by voice" />
              )}
              {line.source === "handoff" && (
                <GitPullRequestArrow
                  className="chat-message-source"
                  size={12}
                  aria-label="Handed over from your Space thread"
                />
              )}
            </>
          )}
          agentProgress={(line, task) =>
            task && line.role === "agent" && line.source !== "voice" && !task.active ? (
              <TurnProgress task={task} />
            ) : null
          }
          footer={questions
            .filter(
              (question) =>
                !question.card &&
                !question.withdrawn_readonly &&
                (question.state === "pending" || question.state === "parked"),
            )
            .map(renderQuestion)}
        />
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
        {(pausedTask || stoppedTask) && (
          <div className="chat-task-inline paused" role="status">
            <span>{(pausedTask ?? stoppedTask)!.status_message}</span>
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
              onClick={() => void control("retry", (pausedTask ?? stoppedTask)!)}
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
      <ChatComposer
        mode="work"
        composer={attachments}
        comments={comments}
        addDisabled={
          attachments.full || attachments.preparing || Boolean(pending && !composer.error)
        }
        field={
          <ThreadTextarea
            draft={draft}
            ref={textareaRef}
            aria-label="Message"
            onChange={(event) => draft.set(event.target.value)}
            onPaste={attachments.paste}
            onKeyDown={(event) => {
              if (!returnSends(event) || event.nativeEvent.isComposing) return;
              event.preventDefault();
              submit();
            }}
          />
        }
        tools={null}
        actions={
          <>
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
              title={sendReason}
              disabled={sendReason !== undefined}
              onClick={submit}
            >
              {pending && !composer.error ? (
                <LoaderCircle className="spin" size={16} />
              ) : (
                <Send size={16} />
              )}
            </button>
          </>
        }
        notice={composer.error && <span role="alert">{composer.error}</span>}
      />
      <AnswerCommentLayers comments={comments} mode="work" />
    </div>
  );
}

function ThreadTextarea({
  draft,
  ...props
}: { draft: ComposerDraft } & Omit<React.ComponentProps<"textarea">, "value">) {
  const value = useComposerDraft(draft);
  return <textarea {...props} value={value} />;
}
