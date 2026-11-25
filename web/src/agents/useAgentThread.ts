import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  profileRunConfig,
  providerModels,
  type AgentConfigSource,
} from "../core/AgentConfigControls";
import { loadSpaceMachines, loadSpaceSkills } from "../core/api";
import { resourceKeyId } from "../core/resourceStore";
import { readinessProfile } from "../projects/projectResources";
import type { SkillSource } from "../core/SkillPicker";
import { memberDraftKey } from "../core/draftStorage";
import { errorMessage } from "../core/errors";
import { EMPTY_SKILL_SELECTION, skillInvocationFields } from "../core/skillPickerModel";
import { useRefreshRuntime, useResource } from "../core/useResource";
import { graphProfile } from "../graph/graphResources";
import { providerChange } from "../projects/providers";
import type { CommentScope } from "../comments/api.ts";
import type { ConversationTurnSubmission } from "../chat/chatWorkspace";
import {
  threadAgentWindowEndpoints,
  type AgentWindowEndpoints,
} from "../agentWindow/agentWindowEndpoints";
import type { AgentWindowScroll } from "../agentWindow/useAgentWindowScroll";
import type {
  AgentQuestion,
  AgentRunConfig,
  AgentSchedule,
  AgentTask,
  AgentThread,
  AgentThreadQueueItem,
  AgentThreadTurnRequest,
  ChatMessage,
  ProjectSnapshot,
  ProviderReadiness,
  WatcherRecord,
} from "../core/types";
import type { CardSource } from "./ApprovalCardStack";
import { browserTimezone, latestTurnId } from "./agentThreadModel";
import {
  loadThreadQueue,
  loadThreadQuestions,
  loadThreadSchedules,
  loadThreadTranscript,
  loadThreadWatchers,
  markThreadRead,
  queueThreadTurn,
  removeQueuedTurn,
  renameThread,
  sendThreadTurn,
  setThreadAgent,
  setThreadBrowser,
  stopThreadWatcher,
} from "./agentThreadsApi";
import { threadWelcome, type ThreadWelcome } from "./threadWelcome";

/** A live turn's task is read this often while the page shows; the rest follows the watermark. */
const LIVE_POLL_MS = 2_000;
/** A turn's detail loads once its row comes this close to the view. */
const TURN_DETAIL_MARGIN = "600px 0px";
const NO_QUESTIONS: AgentQuestion[] = [];
const NO_SCHEDULES: AgentSchedule[] = [];
const NO_WATCHERS: WatcherRecord[] = [];
const NO_QUEUE: AgentThreadQueueItem[] = [];

/** A transcript as loaded for one thread. */
type LoadedThread = {
  messages: ChatMessage[];
  questions: AgentQuestion[];
  latest: AgentTask | null;
  queue: AgentThreadQueueItem[];
  /** The finish of the thread's newest turn when this transcript was read. */
  finishedAt: string | null;
};

/** Each thread's last view, so reopening one shows it at once while it reloads. */
const lastViews = new Map<string, LoadedThread>();

type QueuedMessage = Extract<AgentThreadQueueItem, { kind: "message" }>;

export type AgentPicker = { source: AgentConfigSource; value: AgentRunConfig } | null;

/** What a member's agent thread feeds the agent view: its data, its pieces, and its actions. */
export interface AgentThreadFeed {
  thread: AgentThread;
  endpoints: AgentWindowEndpoints;
  /** The thread's project, when it has one. */
  project: ProjectSnapshot | null;
  messages: ChatMessage[] | null;
  questions: AgentQuestion[];
  tasks: AgentTask[];
  latest: AgentTask | null;
  parents: ReadonlyMap<string, string | null>;
  loadError: string | null;
  /** Waiting sends, when the thread queues them; null when it does not. */
  queue: AgentThreadQueueItem[] | null;
  schedules: AgentSchedule[];
  watchers: WatcherRecord[];
  config: AgentRunConfig | null;
  picker: AgentPicker;
  skillSource: SkillSource | null;
  cardSource: CardSource;
  /** Cards from other projects name their project. */
  cardsRelayed: boolean;
  commentScope: CommentScope;
  draftKey: string | null;
  referencesKey: string | null;
  welcome: ThreadWelcome;
  /** The agent that ran a handed-off turn. */
  handoffAgentName: (threadId: string | null) => string;
  refresh: () => Promise<void>;
  refreshUpcoming: () => Promise<void>;
  reloadPicker: () => void;
  pickAgent: (agent: AgentRunConfig) => Promise<void>;
  start: (
    submission: ConversationTurnSubmission,
    requestId: string,
    queue: boolean,
  ) => Promise<void>;
  control: (action: "pause" | "resume" | "retry", task: AgentTask) => Promise<void>;
  removeQueued: (item: QueuedMessage) => Promise<void>;
  rename: (title: string) => Promise<void>;
  setBrowser: (requested: boolean) => Promise<void>;
  stopWatcher: (watcherId: string) => Promise<void>;
}

/** Reloads that changed nothing keep their old value, so the transcript does not re-render. */
function unchanged<T>(previous: T | undefined, next: T): T {
  return previous !== undefined && JSON.stringify(previous) === JSON.stringify(next)
    ? previous
    : next;
}

/** The first row showing at the top of the transcript, and where it sits. */
function topRow(root: HTMLElement | null): { row: HTMLElement; top: number } | null {
  if (!root) return null;
  const edge = root.getBoundingClientRect().top;
  for (const row of root.querySelectorAll<HTMLElement>(".agent-chat-view-line")) {
    const box = row.getBoundingClientRect();
    if (box.bottom > edge) return { row, top: box.top };
  }
  return null;
}

/** A thread turn's request from the composer's submission; the thread stores its own session. */
function threadTurnRequest(submission: ConversationTurnSubmission): AgentThreadTurnRequest {
  const { config } = submission;
  return {
    message: submission.message.trim(),
    ...submission.attachments,
    ...(submission.commentThreadIds?.length
      ? { comment_thread_ids: [...submission.commentThreadIds] }
      : {}),
    ...(submission.worktree
      ? {
          worktree: true,
          ...(submission.worktreeRepository
            ? { run_truth_scope: [submission.worktreeRepository] }
            : {}),
        }
      : {}),
    ...(submission.worktreeIntegration
      ? { worktree_integration: submission.worktreeIntegration }
      : {}),
    ...skillInvocationFields(
      submission.skills ?? EMPTY_SKILL_SELECTION,
      submission.providerSkillNames ?? [],
    ),
    ...(config.provider ? { provider: config.provider } : {}),
    ...(config.model ? { model: config.model } : {}),
    ...(config.reasoning ? { reasoning: config.reasoning } : {}),
    timezone: browserTimezone(),
  } as AgentThreadTurnRequest;
}

/**
 * What the agent pick reads, the project's readiness or the backend machine's,
 * and its default: the project's Settings profile, or the Space machine's first
 * signed-in provider with that provider's first model.
 */
function useThreadPicker(thread: AgentThread | null): { picker: AgentPicker; reload: () => void } {
  const projectId = thread?.project_id ?? null;
  const { store } = useRefreshRuntime();
  const readinessResource = projectId ? readinessProfile(projectId) : null;
  const readiness = useResource(`thread-picker:${projectId ?? ""}`, readinessResource);
  const [space, setSpace] = useState<AgentPicker>(null);
  /** A sign-in from Settings changes readiness after mount; opening the pick reads it again. */
  const [reads, setReads] = useState(0);
  const readinessId = readinessResource ? resourceKeyId(readinessResource.key) : null;
  const reload = useCallback(() => {
    if (readinessId) store.invalidate(readinessId);
    else setReads((count) => count + 1);
  }, [readinessId, store]);
  const spaceThread = thread !== null && !projectId;
  useEffect(() => {
    if (!spaceThread) return;
    let current = true;
    const load = async (): Promise<AgentPicker> => {
      /** The Space thread runs on the backend's own machine, the one with no host. */
      const machine = (await loadSpaceMachines()).find((item) => !item.host);
      if (!machine) return null;
      /** Every registered provider is offered; a probe only annotates it. */
      const ready = Object.values(machine.provider_readiness) as ProviderReadiness[];
      const first =
        ready.find((item) => item.authenticated)?.provider ?? machine.providers[0]?.provider;
      if (!first) return null;
      const source = {
        provider_readiness: {
          [machine.machine_id]: machine.provider_readiness as Record<string, ProviderReadiness>,
        },
        registry: machine.providers,
      };
      const { model, reasoning = "" } = providerChange(
        providerModels(source, machine.machine_id, first),
        first,
        "",
      );
      return { source, value: { provider: first, model, reasoning, run_on: machine.machine_id } };
    };
    /** Without readiness the pick stays away and the backend picks the provider. */
    load()
      .then((loaded) => current && setSpace(loaded))
      .catch(() => current && setSpace(null));
    return () => {
      current = false;
    };
  }, [spaceThread, reads]);
  const loaded = readiness.value;
  const projectPicker = useMemo<AgentPicker>(
    () =>
      loaded
        ? {
            source: { provider_readiness: loaded.provider_readiness },
            value: profileRunConfig(loaded.agent_profiles.project_chat, loaded.run_on),
          }
        : null,
    [loaded],
  );
  return { picker: projectId ? projectPicker : thread ? space : null, reload };
}

/**
 * One member agent thread's data and actions for the agent view. The thread's
 * routes, Space or project, are the API module's choice; the view stays the same.
 * The view mounts one per thread, so nothing here answers for another thread.
 */
export function useAgentThread({
  thread,
  threads,
  project,
  actorId,
  revision,
  onThreadChanged,
  linesRef,
  scroll,
}: {
  thread: AgentThread | null;
  threads?: readonly AgentThread[];
  project?: ProjectSnapshot | null;
  actorId: string | null;
  revision: string | null;
  onThreadChanged?: (thread: AgentThread) => void;
  linesRef: RefObject<HTMLDivElement | null>;
  scroll: RefObject<AgentWindowScroll | null>;
}): AgentThreadFeed | null {
  const threadId = thread?.thread_id ?? null;
  const projectId = thread?.project_id ?? null;
  const threadRef = useRef(thread);
  threadRef.current = thread;
  const changed = useRef(onThreadChanged);
  changed.current = onThreadChanged;
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const [view, setView] = useState<LoadedThread | null>(() =>
    threadId ? (lastViews.get(threadId) ?? null) : null,
  );
  const [fresh, setFresh] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [schedules, setSchedules] = useState<AgentSchedule[]>(NO_SCHEDULES);
  const [watchers, setWatchers] = useState<WatcherRecord[]>(NO_WATCHERS);
  const [turnTasks, setTurnTasks] = useState<ReadonlyMap<string, AgentTask>>(() => new Map());
  const [lineage, setLineage] = useState<ReadonlyMap<string, string | null>>(() => new Map());
  const [spaceSkills, setSpaceSkills] = useState<SkillSource | null>(null);
  const { picker, reload: reloadPicker } = useThreadPicker(thread);
  const endpoints = useMemo(
    () => (thread ? threadAgentWindowEndpoints(thread) : null),
    /** The routes follow the thread's identity, not its changing row. */
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by id and project
    [threadId, projectId],
  );
  const endpointsRef = useRef(endpoints);
  endpointsRef.current = endpoints;
  const graph = useResource(
    `agent-window:${threadId ?? ""}:references`,
    projectId && !project ? graphProfile(projectId) : null,
  );
  const snapshot = project ?? graph.value ?? null;

  const messages = view?.messages ?? null;
  const latest = view?.latest ?? null;

  /** Every answer older than one already shown is dropped. */
  const seq = useRef({ started: 0, shown: 0 });
  /** A send's own turn leads until the thread row reports a newer latest turn. */
  const sentTurn = useRef<{ operationId: string; after: string | null } | null>(null);
  const refresh = useCallback(async () => {
    const current = threadRef.current;
    const owner = endpointsRef.current;
    if (!current || !owner) return;
    const mine = ++seq.current.started;
    try {
      const [transcript, asked, waiting] = await Promise.all([
        loadThreadTranscript(current),
        loadThreadQuestions(current),
        loadThreadQueue(current),
      ]);
      const sent = sentTurn.current;
      /** A running project turn writes its member message only when it settles. */
      const turnId =
        sent && sent.after === current.latest_operation_id
          ? sent.operationId
          : (current.latest_operation_id ?? latestTurnId(transcript.messages));
      const latestTask = turnId ? await owner.loadTask(turnId) : null;
      if (!alive.current || mine <= seq.current.shown) return;
      seq.current.shown = mine;
      setView((previous) => {
        const next = {
          messages: unchanged(previous?.messages, transcript.messages),
          questions: unchanged(previous?.questions, asked),
          latest: unchanged(previous?.latest ?? undefined, latestTask),
          queue: unchanged(previous?.queue, waiting.items),
          finishedAt: current.last_finished_at,
        };
        const same =
          previous &&
          next.messages === previous.messages &&
          next.questions === previous.questions &&
          next.latest === previous.latest &&
          next.queue === previous.queue &&
          next.finishedAt === previous.finishedAt;
        if (same) return previous;
        lastViews.set(current.thread_id, next);
        return next;
      });
      setFresh(true);
      setLoadError(null);
    } catch (failure) {
      if (alive.current && mine > seq.current.shown) setLoadError(errorMessage(failure));
    }
  }, []);
  /** Watermark moves reload one at a time; a move during a reload runs it once more after. */
  const reloading = useRef<{ again: boolean } | null>(null);
  const reloadOnce = useCallback(async () => {
    if (reloading.current) {
      reloading.current.again = true;
      return;
    }
    const entry = { again: false };
    reloading.current = entry;
    do {
      entry.again = false;
      await refresh();
    } while (entry.again && alive.current);
    reloading.current = null;
  }, [refresh]);

  const refreshUpcoming = useCallback(async () => {
    const current = threadRef.current;
    if (!current) return;
    const [list, watching] = await Promise.allSettled([
      loadThreadSchedules(current.thread_id),
      loadThreadWatchers(current),
    ]);
    if (!alive.current) return;
    if (list.status === "fulfilled") setSchedules(list.value.schedules);
    if (watching.status === "fulfilled") setWatchers(watching.value);
  }, []);

  useEffect(() => {
    if (!threadId) return;
    void reloadOnce();
    void refreshUpcoming();
  }, [reloadOnce, refreshUpcoming, threadId, revision]);

  /**
   * While a turn runs, only its task is read, one read at a time and never while the
   * page is hidden; its settling reloads the thread for the outcome.
   */
  const liveTaskId = latest?.active ? latest.operation_id : null;
  useEffect(() => {
    const owner = endpointsRef.current;
    if (!liveTaskId || !owner) return;
    let stopped = false;
    let reading = false;
    let timer: number | undefined;
    const beat = async () => {
      window.clearTimeout(timer);
      if (stopped || reading || document.visibilityState === "hidden") return;
      reading = true;
      try {
        const task = await owner.loadTask(liveTaskId);
        if (stopped) return;
        setView((known) => {
          if (known?.latest?.operation_id !== liveTaskId) return known;
          const next = unchanged(known.latest, task);
          return next === known.latest ? known : { ...known, latest: next };
        });
        if (!task.active) void refresh();
      } catch {
        /** The next beat reads it again. */
      } finally {
        reading = false;
      }
      if (!stopped) timer = window.setTimeout(() => void beat(), LIVE_POLL_MS);
    };
    const onVisibility = () => void beat();
    document.addEventListener("visibilitychange", onVisibility);
    timer = window.setTimeout(() => void beat(), LIVE_POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [liveTaskId, refresh]);

  const tasks = useMemo(() => {
    const ids = new Set(messages?.map((message) => message.operation_id) ?? []);
    const result = [...ids].flatMap((id) => {
      const task = id && id !== latest?.operation_id ? turnTasks.get(id) : undefined;
      return task ? [task] : [];
    });
    if (latest) result.push(latest);
    return result;
  }, [latest, messages, turnTasks]);

  /** A Resume or Retry names only its parent; the chain tells which turn it continues. */
  let unlinked = latest?.parent_operation_id ?? null;
  while (unlinked && lineage.has(unlinked)) unlinked = lineage.get(unlinked) ?? null;
  useEffect(() => {
    const owner = endpointsRef.current;
    if (!unlinked || !owner) return;
    let current = true;
    owner.loadTask(unlinked).then(
      (task) =>
        current &&
        setLineage((known) =>
          new Map(known).set(task.operation_id, task.parent_operation_id ?? null),
        ),
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, [unlinked]);

  /**
   * A turn's detail loads once, when its row nears the view. A failed or unsettled
   * read is tried again only after the watermark moves.
   */
  const historyTurnIds = useMemo(
    () => new Set(messages?.flatMap((message) => message.operation_id ?? []) ?? []),
    [messages],
  );
  const [nearTurns, setNearTurns] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    const root = linesRef.current;
    if (!root || !threadId) return;
    const observer = new IntersectionObserver(
      (entries) =>
        setNearTurns((known) => {
          const next = new Set(known);
          for (const entry of entries) {
            const id = (entry.target as HTMLElement).dataset.taskId!;
            if (entry.isIntersecting) next.add(id);
            else next.delete(id);
          }
          return next.size === known.size && [...next].every((id) => known.has(id)) ? known : next;
        }),
      { root, rootMargin: TURN_DETAIL_MARGIN },
    );
    for (const row of root.querySelectorAll<HTMLElement>(".agent-chat-view-line[data-task-id]"))
      observer.observe(row);
    return () => observer.disconnect();
  }, [linesRef, threadId, messages, tasks]);
  /** A loaded turn can grow a row above the reader; the row at the top stays where it was. */
  const readerAnchor = useRef<{ row: HTMLElement; top: number } | null>(null);
  useLayoutEffect(() => {
    const anchor = readerAnchor.current;
    readerAnchor.current = null;
    if (anchor && linesRef.current)
      linesRef.current.scrollTop += anchor.row.getBoundingClientRect().top - anchor.top;
  }, [linesRef, turnTasks]);
  const triedTurns = useRef(new Set<string>());
  useEffect(() => {
    triedTurns.current.clear();
  }, [revision]);
  const latestId = latest?.operation_id ?? null;
  useEffect(() => {
    const owner = endpointsRef.current;
    if (!owner) return;
    const wanted = [...nearTurns].filter(
      (id) =>
        historyTurnIds.has(id) &&
        id !== latestId &&
        !turnTasks.has(id) &&
        !triedTurns.current.has(id),
    );
    if (!wanted.length) return;
    for (const id of wanted) triedTurns.current.add(id);
    void Promise.all(wanted.map((id) => owner.loadTask(id).catch(() => null))).then((loaded) => {
      const settled = loaded.filter((task): task is AgentTask => Boolean(task?.settled));
      if (!settled.length || !alive.current) return;
      readerAnchor.current = scroll.current?.shouldStickToBottomRef.current
        ? null
        : topRow(linesRef.current);
      setTurnTasks((known) => {
        const next = new Map(known);
        for (const task of settled) next.set(task.operation_id, task);
        return next;
      });
    });
  }, [nearTurns, historyTurnIds, turnTasks, latestId, linesRef, scroll]);

  /** Reading marks the thread read through the turn its reloaded transcript shows. */
  const newest = view?.finishedAt ?? null;
  const unread = thread?.unread ?? false;
  useEffect(() => {
    if (!threadId || !unread || !newest || !fresh) return;
    markThreadRead(threadId, newest).then(
      (row) => changed.current?.(row),
      () => undefined,
    );
  }, [unread, threadId, fresh, newest]);

  useEffect(() => {
    if (!threadId || projectId || spaceSkills) return;
    loadSpaceSkills().then(
      (skills) => alive.current && setSpaceSkills(skills),
      () => undefined,
    );
  }, [threadId, projectId, spaceSkills]);

  const commentScope = useMemo<CommentScope>(
    () =>
      projectId
        ? { kind: "project", projectId, threadId: threadId ?? undefined }
        : { kind: "space", threadId: threadId ?? "" },
    [projectId, threadId],
  );
  const cardSource = useMemo<CardSource>(
    () => ({ threadId: projectId ? threadId : null }),
    [projectId, threadId],
  );

  if (!thread || !endpoints) return null;
  /**
   * The thread stores its pick and every turn runs it, retries and wakes included;
   * until the member picks, the default shows and the next send stores it. A send
   * stores no machine, so that pick runs on, and reads readiness from, the default's.
   */
  const config = thread.agent
    ? { ...thread.agent, run_on: thread.agent.run_on || picker?.value.run_on || "" }
    : (picker?.value ?? null);
  const after = async () => {
    if (alive.current) await refresh();
  };
  return {
    thread,
    endpoints,
    project: snapshot,
    messages,
    questions: view?.questions ?? NO_QUESTIONS,
    tasks,
    latest,
    parents: lineage,
    loadError,
    queue: view?.queue ?? NO_QUEUE,
    schedules,
    watchers,
    config,
    picker,
    skillSource: projectId ? snapshot : spaceSkills,
    cardSource,
    cardsRelayed: !projectId,
    commentScope,
    draftKey: memberDraftKey(actorId, `agent-window:${endpoints.scopeKey}`),
    referencesKey: memberDraftKey(actorId, `agent-window-references:${endpoints.scopeKey}`),
    welcome: threadWelcome(
      thread,
      threads?.find((item) => item.project_id && item.live_state !== "closed")?.project_name ??
        null,
    ),
    handoffAgentName: (id) => {
      const target = threads?.find((item) => item.thread_id === id);
      return target?.agent_name ?? target?.project_name ?? "Project agent";
    },
    refresh,
    refreshUpcoming,
    reloadPicker,
    pickAgent: async (agent) => {
      const target = thread;
      changed.current?.({ ...target, agent });
      try {
        changed.current?.(await setThreadAgent(target.thread_id, agent));
      } catch (failure) {
        changed.current?.(target);
        throw failure;
      }
    },
    start: async (submission, requestId, queue) => {
      const request = threadTurnRequest(submission);
      const { operation_id } = queue
        ? await queueThreadTurn(thread, request, requestId)
        : await sendThreadTurn(thread, request, requestId);
      if (operation_id)
        sentTurn.current = { operationId: operation_id, after: thread.latest_operation_id };
      await after();
    },
    control: async (action, task) => {
      try {
        await endpoints.control(task.operation_id, action, crypto.randomUUID());
      } finally {
        void after();
      }
    },
    removeQueued: async (item) => {
      await removeQueuedTurn(thread, item.queue_id);
      void after();
    },
    rename: async (title) => {
      changed.current?.(await renameThread(thread.thread_id, title));
    },
    setBrowser: async (requested) => {
      const result = await setThreadBrowser(thread.thread_id, requested);
      changed.current?.({ ...thread, ...result });
    },
    stopWatcher: async (watcherId) => {
      await stopThreadWatcher(thread, watcherId);
      await refreshUpcoming();
    },
  };
}
