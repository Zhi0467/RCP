import { useAgentUpcoming } from "../agents/useAgentUpcoming";
import { queueThreadTurn, removeQueuedTurn, threadPath } from "../agents/agentThreadsApi";
import {
  AgentWindowTranscript,
  InlineTaskProgress,
  TurnProgress,
} from "../agentWindow/AgentWindowTranscript";
import {
  useAgentWindowLines,
  type PendingAgentWindowTurn,
} from "../agentWindow/useAgentWindowLines";
import { useAgentWindowArtifacts } from "../agentWindow/useAgentWindowArtifacts";
import { AgentWindowLines } from "../agentWindow/AgentWindowLines";
import type { AgentWindowScroll } from "../agentWindow/useAgentWindowScroll";
import { projectAgentWindowEndpoints } from "../agentWindow/agentWindowEndpoints";
import { ProviderSwitchHint } from "./ProviderSwitchHint";
import { ChatBranchPicker, ChatOriginLink } from "./ChatBranchPicker";
import { ChatComposer, ComposerField } from "./ChatComposer";
import { ChatArtifactsPane, useChatArtifacts } from "./ChatArtifactsPane";
import { ChatHeaderMenu, type ChatHeaderAction } from "./ChatHeaderMenu";
import { AgentHeader, AgentPickRow } from "./AgentHeader";
import type { ChatAgentSetup, ChatOrigin } from "../core/types";
import { MAIN_GRAPH, sameGraphTarget } from "../core/graphTarget";
import { EpisodePlanCard } from "../agents/EpisodePlanCard";
import { isPlanCardQuestion, planCardAction, planGraphTarget } from "../agents/experimentPlanModel";
import {
  AgentCardsUnavailable,
  ApprovalCardStack,
  askedCardKey,
  dockStackCards,
  proposalCardKey,
  useAgentCards,
  type StackCard,
} from "../agents/ApprovalCardStack";
import { useComposerAttachments } from "./composerAttachments";
import { referenceDraftKey } from "../core/projectReferences";
import { QuestionCard } from "./QuestionCard";
import { useQuestions } from "./useQuestions";
import { questionIsOpen } from "./questions";
import { ExternalJobRow } from "../experiments/ExternalJobRow";
import {
  CalendarClock,
  Files,
  GitPullRequestArrow,
  ListPlus,
  LoaderCircle,
  Mic,
  MicOff,
  Play,
  RadioTower,
  RotateCcw,
  Send,
  Square,
  X,
} from "lucide-react";
import {
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import {
  ApiError,
  loadServiceConnections,
  loadConnectionModels,
  setChatTitle,
  setChatArchived,
  transcribeAudio,
} from "../core/api";
import {
  type TaskTranscriptLine,
  isActiveTask,
  pausedTaskText,
  resolvedChatSessionId,
  relatedChatTasks,
  resumablePausedChatTask,
  stoppedByMember,
} from "../agents/agentTasks";
import { handoffReport, localClock } from "../agents/agentThreadModel";
import { useAgentThread } from "../agents/useAgentThread";
import { AgentThreadWelcome } from "../agents/AgentThreadWelcome";
import {
  FreshSessionMarker,
  HandoffResult,
  SchedulesPopover,
  ThreadQueue,
  type QueuedMessage,
} from "./agentViewPieces";
import { AgentRename } from "./AgentRename";
import type { VoiceAgent } from "../voice/useVoiceAgent";
import {
  chatDraftStorageKey,
  chatIdForTask,
  latestPersistedChatConfig,
  originRunConfig,
  retryRefillsComposer,
  startConversationTurn,
  type ConversationTurnSubmission,
  type StartConversationTask,
} from "./chatWorkspace";
import { arrowScrollsTranscript, returnSends } from "../ui/composerKeys";
import { replaceTextSpan } from "./chatInput";
import { useReplyComments } from "./ReplyComments";
import { chatDockScope, useCommentDock } from "../comments/react/CommentDock";
import { sendKeys } from "../comments/react/threadModel";
import { useInlineArtifactComments } from "./inlineArtifactComments";
import type { DockThreads } from "../comments/react/useComments";
import type { CommentScope } from "../comments/api.ts";
import { createComposerDraft, useComposerDraft, type ComposerDraft } from "./composerDraft";
import {
  chooseRecordingFormat,
  liveDictationSpan,
  serviceConnectionFailure,
  type DictationSpan,
} from "../voice/dictation";
import { errorMessage } from "../core/errors";
import type { GlossaryIndex } from "../graph/glossary";
import { claimMicrophone, type MicrophoneClaim } from "../voice/microphone";
import {
  createQuietMeter,
  joinDictatedText,
  addKeptSpeech,
  keptSpeech,
  keptSpeechNeedsService,
  NetworkDictationSession,
  resolveKeptPieces,
  setKeptSpeech,
  useKeptSpeech,
  type KeptPiece,
  type KeptSpeech,
} from "../voice/networkDictation";
import {
  graphConditionLabel,
  isExternalWatcherRecord,
  visibleChatWatchers,
  watcherLastObservedAt,
} from "../experiments/runProjection";
import {
  type DictationResultEvent,
  type DictationStateEvent,
  isDesktopRuntime,
  listenDesktopEvent,
  startDesktopDictation,
  stopDesktopDictation,
} from "../core/desktopRuntime";
import type {
  AgentExecutionProfile,
  AgentQuestion,
  AgentRunConfig,
  AgentTask,
  AgentThread,
  ChatMessage,
  ProposalCardView,
  SteerRequest,
  GraphNode,
  GraphTargetRef,
  GraphUpdateRecovery,
  ProjectSnapshot,
  ServiceConnection,
  WatcherRecord,
  WorktreeIntegrationOption,
} from "../core/types";
import { launchProviderReady, profileRunConfig } from "../core/AgentConfigControls";
import { useSkillPicker } from "../core/SkillPicker";
import { EMPTY_SKILL_SELECTION, skillInvocationFields } from "../core/skillPickerModel";
import { AgentStatusStrip } from "./AgentStatusStrip";
import { BrowserToggle, ChatBrowserControl } from "../core/BrowserControls";
import { WorktreeChooser, WorktreeControls, useConversationWorktree } from "./WorktreeControls";
import { worktreeRepositoryChoices } from "./worktreeRepositories";

interface Props {
  /** A shared chat's project; a member thread's own project when the host has it loaded. */
  project?: ProjectSnapshot | null;
  /** The signed-in member whose persisted drafts this chat restores and saves. */
  actorId: string | null;
  graphTarget?: GraphTargetRef;
  node?: GraphNode | null;
  nodes?: Readonly<Record<string, GraphNode>>;
  glossaryIndex?: GlossaryIndex;
  conversationTitle?: string;
  /** Workspace title and status; shares one row with the chat controls. */
  header?: ReactNode;
  /** Chat actions the host adds to the header menu, before New session. */
  headerActions?: readonly ChatHeaderAction[];
  /** Controls the host adds to the header menu after its actions. */
  headerMenuItems?: ReactNode;
  headerState?: string;
  tasks?: AgentTask[];
  watchers?: WatcherRecord[];
  historyMessages?: ChatMessage[];
  origin?: ChatOrigin | null;
  /** The chat summary's newest turn, which the loaded history may not reach yet. */
  agentSetup?: ChatAgentSetup | null;
  chatId: string;
  presentation?: "floating" | "workspace";
  fixedConversation?: boolean;
  readOnly?: boolean;
  archived?: boolean;
  onRestore?: () => void;
  readOnlyNotice?: ReactNode;
  /** Questions belong to this episode rather than the chat, as an orchestrator's do. */
  questionEpisodeId?: string;
  allowArtifactComments?: boolean;
  reviewPending?: boolean;
  graphChangesDisabled?: boolean;
  onStartTask?: StartConversationTask;
  onInspectTask?: (taskId: string) => void;
  onOpenInbox?: () => void;
  onRepairGraphUpdate?: (taskId: string, action?: GraphUpdateRecovery) => Promise<void>;
  onOpenNode?: (nodeId: string) => void;
  onStopWatcher?: (watcherId: string) => void;
  onNewSession?: () => void;
  onClose?: () => void;
  onResumeTask?: (task: AgentTask) => void;
  onRetryTask?: (task: AgentTask) => void;
  onRefreshTask?: (taskId: string) => Promise<AgentTask>;
  composerRetry?: AgentTask | null;
  onComposerRetryTaken?: () => void;
  /** The Settings profile a turn starts from; a chat's own surface by default. */
  profileName?: AgentExecutionProfile;
  /** What follows a turn's last line, such as a Paper turn's changed files. */
  turnTrail?: (taskId: string) => ReactNode;
  /** Set to this composer's thread sender, which the host's own comment threads ride. */
  threadSender?: RefObject<DockThreads | null>;
  /** A member's own agent thread, which loads its own transcript and turns. */
  thread?: AgentThread | null;
  /** The member's threads, which name the agent a handoff came from. */
  threads?: readonly AgentThread[];
  /** The member's agent-thread watermark; a change reloads the thread. */
  revision?: string | null;
  onThreadChanged?: (thread: AgentThread) => void;
  /** The rail's fold or the phone's back control, at the header's start. */
  leading?: ReactNode;
  /** Controls the host adds before the header's own. */
  headerTools?: ReactNode;
  /** The space voice agent; it takes the mic in place of dictation. */
  voice?: VoiceAgent | null;
}

export { pendingTurnShownByHistory } from "../agentWindow/useAgentWindowLines";

interface PendingChatTurn extends PendingAgentWindowTurn {
  /** The start's Idempotency-Key, kept across a Resend. */
  requestId: string;
  /** The composer text it was sent from, which a failed send puts back. */
  draft: string;
  submission: ConversationTurnSubmission;
  submittedOverride: { chatId: string; config: AgentRunConfig } | null;
  /** Waits in the thread's queue instead of starting; it never shows as a transcript line. */
  queue?: true;
  /** The queued request's content, which a retry after a refusal sends under the same key. */
  queued?: string;
}

/** What a Send addresses: the exact running attempt it steers, a new turn with
 *  its launch settings, or nothing while an unsteerable turn runs. */
function sendTarget(task: AgentTask | undefined, launch: string, active: boolean): string {
  if (task) return `steer:${task.operation_id}:${task.attempt}:${task.steer_turn_id}`;
  return active ? "busy" : `new:${launch}`;
}

/** A network dictation session; `session` is null until the microphone opens. */
interface NetworkDictation {
  sessionId: string;
  session: NetworkDictationSession | null;
  /** Where a detached session's kept speech continues, while the draft is unchanged. */
  resume?: { draft: string; at: number } | null;
}

const EMPTY_WATCHERS: WatcherRecord[] = [];
/** A turn whose start failed after its view closed, kept for that view's Resend. */
const unsentTurns = new Map<string, PendingChatTurn>();
const EMPTY_HISTORY: ChatMessage[] = [];
const NO_HEADER_ACTIONS: readonly ChatHeaderAction[] = [];

// macOS dictation stops at 55 s, counting down the last 10; network dictation
// has no limit (see networkDictation.ts).
const DICTATION_SEGMENT_MS = 55_000;
const DICTATION_COUNTDOWN_MS = 10_000;
const SYSTEM_DICTATION_ELSEWHERE =
  "Your dictation service is macOS, which works only in the desktop app. Choose a connection in Space settings, under Dictation and voice.";

// One empty map, so a chat without graph nodes keeps its answers' parse memoized.
const NO_NODES: Readonly<Record<string, GraphNode>> = {};

const NO_TASKS: AgentTask[] = [];
const BLANK_CONFIG: AgentRunConfig = { provider: "", model: "", reasoning: "", run_on: "" };

export function AgentChatView({
  project: requestedProject = null,
  actorId,
  graphTarget = MAIN_GRAPH,
  node,
  nodes = NO_NODES,
  glossaryIndex,
  conversationTitle,
  header,
  headerActions = NO_HEADER_ACTIONS,
  headerMenuItems,
  headerState,
  tasks = NO_TASKS,
  watchers: hostWatchers = EMPTY_WATCHERS,
  historyMessages = EMPTY_HISTORY,
  origin,
  agentSetup,
  chatId,
  presentation = "floating",
  fixedConversation = false,
  readOnly: requestedReadOnly = false,
  archived = false,
  onRestore,
  allowArtifactComments: requestedArtifactComments = false,
  readOnlyNotice,
  questionEpisodeId,
  reviewPending = false,
  graphChangesDisabled = false,
  onStartTask,
  onInspectTask,
  onOpenInbox,
  onRepairGraphUpdate,
  onOpenNode,
  onStopWatcher: hostStopWatcher,
  onNewSession,
  onClose,
  onResumeTask: hostResumeTask,
  onRetryTask: hostRetryTask,
  onRefreshTask: hostRefreshTask,
  composerRetry = null,
  onComposerRetryTaken,
  turnTrail,
  threadSender,
  profileName,
  thread = null,
  threads,
  revision = null,
  onThreadChanged,
  leading,
  headerTools,
  voice = null,
}: Props) {
  const chatLinesRef = useRef<HTMLDivElement | null>(null);
  const scroll = useRef<AgentWindowScroll>(null);
  const feed = useAgentThread({
    thread,
    threads,
    project: requestedProject,
    actorId,
    revision,
    onThreadChanged,
    linesRef: chatLinesRef,
    scroll,
  });
  const project = feed ? feed.project : requestedProject;
  const isArchived = archived || Boolean(feed?.thread.archived);
  const readOnly = isArchived || requestedReadOnly;
  const allowArtifactComments = !isArchived && requestedArtifactComments;
  const surface = node ? "node_chat" : "project_chat";
  const relatedTasks = useMemo(
    () => feed?.tasks ?? relatedChatTasks(tasks, surface, node?.id, chatId),
    [feed?.tasks, chatId, node?.id, surface, tasks],
  );
  const upcoming = useAgentUpcoming(
    !feed && (relatedTasks.length > 0 || historyMessages?.length) ? chatId : null,
    revision ?? relatedTasks.at(-1)?.updated_at ?? null,
  );
  const projectId = project?.id ?? null;
  const chatEndpoints = useMemo(
    () => (projectId ? projectAgentWindowEndpoints(projectId, chatId) : null),
    [projectId, chatId],
  );
  const endpoints = feed?.endpoints ?? chatEndpoints!;
  const listsArtifacts =
    presentation === "workspace" && !feed && projectId !== null && profileName !== "paper_coach";
  const chatArtifacts = useChatArtifacts(listsArtifacts ? projectId : null, chatId);
  const [artifactsOpen, setArtifactsOpen] = useState(false);
  const artifactsPane = listsArtifacts && chatArtifacts.length > 0;
  const questionApiBase = endpoints.questionsApiBase;
  const chatQuestions = useQuestions(
    questionApiBase ?? "",
    questionEpisodeId ? "episode" : "chat",
    feed ? undefined : (questionEpisodeId ?? chatId),
  );
  const questionState = feed
    ? {
        questions: feed.questions,
        error: null,
        refresh: () => void feed.refresh(),
      }
    : chatQuestions;
  const [steeringMessages, setSteeringMessages] = useState<{
    chatId: string;
    messages: ChatMessage[];
  }>({ chatId, messages: [] });
  useEffect(() => {
    setSteeringMessages({ chatId, messages: [] });
  }, [chatId]);
  const loadedMessages = feed ? (feed.messages ?? EMPTY_HISTORY) : historyMessages;
  const displayedMessages = useMemo(() => {
    const messages = new Map(loadedMessages.map((message) => [message.message_id, message]));
    if (steeringMessages.chatId === chatId) {
      steeringMessages.messages.forEach((message) => messages.set(message.message_id, message));
    }
    return [...messages.values()];
  }, [chatId, loadedMessages, steeringMessages]);
  const [pendingTurn, setPendingTurn] = useState<PendingChatTurn | null>(
    () => unsentTurns.get(endpoints.scopeKey) ?? null,
  );
  const { transcript, statusTasks, compactionByLine, continuedTaskIds, latestGraphReceiptLineIds } =
    useAgentWindowLines({
      messages: displayedMessages,
      tasks: relatedTasks,
      pendingTurn,
      parents: feed?.parents,
    });
  const shownLines = useMemo(() => transcript.filter((line) => !line.running), [transcript]);
  const liveLine = transcript.find((line) => line.running);
  const watcherTrigger = (line: TaskTranscriptLine) =>
    line.trigger !== "watcher" || handoffReport(line) ? null : (
      <span className="chat-turn-trigger watcher">Watcher</span>
    );
  const profile = project?.agent_profiles[profileName ?? surface] ?? null;
  const derivedConfig = useMemo(
    () =>
      feed
        ? (feed.config ?? BLANK_CONFIG)
        : latestPersistedChatConfig(
            historyMessages,
            relatedTasks,
            originRunConfig(
              origin,
              profile ? profileRunConfig(profile, project!.run_on) : BLANK_CONFIG,
            ),
            agentSetup,
          ),
    [feed, agentSetup, historyMessages, origin, profile, project, relatedTasks],
  );
  const configSource = feed ? (feed.picker?.source ?? null) : project;
  const [configOverride, setConfigOverride] = useState<{
    chatId: string;
    config: AgentRunConfig;
  } | null>(null);
  const pendingFork = Boolean(
    origin?.pending &&
    !relatedTasks.some((task) => task.native_session_id) &&
    !historyMessages.some((message) => message.native_session_id),
  );
  const config = useMemo(
    () =>
      configOverride?.chatId === chatId
        ? { ...configOverride.config, run_on: derivedConfig.run_on }
        : derivedConfig,
    [chatId, configOverride, derivedConfig],
  );
  const worktreeRepositories = project ? worktreeRepositoryChoices(project, config.run_on) : [];
  const [worktreeRepositoryPick, setWorktreeRepositoryPick] = useState<{
    chatId: string;
    alias: string;
  } | null>(null);
  const worktreeRepository =
    worktreeRepositoryPick?.chatId === chatId &&
    worktreeRepositories.includes(worktreeRepositoryPick.alias)
      ? worktreeRepositoryPick.alias
      : (worktreeRepositories[0] ?? null);
  const newestTask = relatedTasks.at(-1);
  const worktree = useConversationWorktree(
    project?.id ?? "",
    chatId,
    node ? "node" : "project",
    node?.id ?? null,
    config.run_on,
    worktreeRepository ? [worktreeRepository] : [],
    newestTask ? `${newestTask.operation_id}:${newestTask.updated_at}` : "",
    Boolean(project && config.run_on && feed?.messages !== null) && !readOnly,
    graphTarget,
  );
  const draftKey = feed ? feed.draftKey : chatDraftStorageKey(actorId, project!.id, chatId);
  const [draft] = useState(() => createComposerDraft(readStorage(draftKey) ?? ""));
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const canSendThreads = !readOnly || allowArtifactComments;
  const commentDock = useCommentDock(chatDockScope(project?.id ?? "space", chatId));
  const dockKeys = useRef(sendKeys());
  const [supersedes, setSupersedes] = useState<{ chatId: string; operationId: string } | null>(
    null,
  );
  const supersededId = supersedes?.chatId === chatId ? supersedes.operationId : null;
  const optionCount = Number(worktree.chosen);
  const referencesKey = feed
    ? feed.referencesKey
    : referenceDraftKey(actorId, project!.id, project!.graph_target ?? MAIN_GRAPH, chatId);
  const composer = useComposerAttachments({
    project,
    attachmentsUrl: endpoints.attachmentsUrl,
    scopeId: chatId,
    referencesKey,
    input: textareaRef,
    message: {
      get: () => draft.get(),
      set: (next) => {
        if (dictating) stopDictation(true);
        updateMessage(next);
      },
    },
    filesDisabled: readOnly,
    referencesDisabled: submitting,
    onError: setSubmitError,
  });
  const readyAttachments = composer.ready;
  const [dictationState, setDictationState] = useState<
    "idle" | "starting" | "preparing" | "recording" | "stopping" | "transcribing" | "error"
  >("idle");
  const [dictationError, setDictationError] = useState<string | null>(null);
  const [dictationEngine, setDictationEngine] = useState<DictationStateEvent["engine"] | null>(
    null,
  );
  const [dictationService, setDictationService] = useState<string | null>(null);
  const [dictationUnavailable, setDictationUnavailable] = useState<string | null>(null);
  const [dictationNote, setDictationNote] = useState<string | null>(null);
  const [dictationStartedAt, setDictationStartedAt] = useState<number | null>(null);
  const [dictationPiecesBusy, setDictationPiecesBusy] = useState(0);
  const [discardPrompt, setDiscardPrompt] = useState<"dictate" | "send" | null>(null);
  const [dictationDeadline, setDictationDeadline] = useState<number | null>(null);
  const [dictationClock, setDictationClock] = useState(() => Date.now());
  const [sendAfterDictation, setSendAfterDictation] = useState<{
    target: string;
  } | null>(null);
  const [stop, setStop] = useState<{
    scope: string;
    operationId: string;
    attempt: number;
    sends: boolean;
    message: string;
    sessionId: string | null;
    result?: true | Error;
  } | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [renamedTitle, setRenamedTitle] = useState<string | null>(null);
  const dockRef = useRef<HTMLDivElement>(null);
  const dictationSpanRef = useRef<DictationSpan | null>(null);
  const dictationTimerRef = useRef<number | null>(null);
  const microphoneRef = useRef<{ sessionId: string; claim: MicrophoneClaim } | null>(null);
  const networkDictationRef = useRef<NetworkDictation | null>(null);
  const [repairingTaskId, setRepairingTaskId] = useState<string | null>(null);
  const [repairErrors, setRepairErrors] = useState<Map<string, string>>(() => new Map());
  const [watchersOpen, setWatchersOpen] = useState(false);
  const steersNow = relatedTasks.some(
    (task) => task.active && task.steer_visible && task.can_steer && task.steer_turn_id,
  );
  const [queueFailure, setQueueFailure] = useState<{
    requestId: string;
    request: string;
    reason: string;
  } | null>(null);
  const skills = useSkillPicker({
    source: steersNow ? null : feed ? feed.skillSource : project,
    target: config,
    message: draft,
    input: textareaRef,
    onComplete: () => setSubmitError(null),
  });
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const relatedActive = relatedTasks.some(isActiveTask);
  const cards = useAgentCards(
    feed ? feed.cardSource : questionEpisodeId ? null : { projectId: project!.id, chatId },
    feed ? revision : null,
  );
  const cardsChanged = async () => {
    await cards.refetch();
    questionState.refresh();
  };
  const dockedPlans = new Set([
    ...cards.asked.map((question) => question.question_id),
    ...cards.plans.map((question) => question.question_id),
    ...(cards.loading
      ? questionState.questions.flatMap((question) =>
          (question.card || isPlanCardQuestion(question)) && question.state === "pending"
            ? [question.question_id]
            : [],
        )
      : []),
  ]);
  const relayed = feed?.cardsRelayed ?? false;
  const proposalStackCard = (view: ProposalCardView): StackCard => ({
    kind: "proposal",
    key: proposalCardKey(view),
    view,
    decided: cards.decisionOf(view),
    showProject: relayed,
  });
  const askedStackCard = (question: AgentQuestion, projectName: string | null): StackCard => ({
    kind: "asked",
    key: askedCardKey(question),
    question,
    projectName,
  });
  const dockCards: StackCard[] = [
    ...cards.proposals.filter((view) => !view.stale).map(proposalStackCard),
    ...cards.asked.map((question) =>
      askedStackCard(question, relayed ? question.project_name : null),
    ),
    ...dockStackCards(cards.plans, cards.episodes, cards.questions),
  ];
  const renderPlanCard = (question: AgentQuestion) => {
    const action = planCardAction(question.card)!;
    const here =
      action.kind === "experiment_start" &&
      onOpenNode &&
      sameGraphTarget(planGraphTarget(action), graphTarget) &&
      Boolean(nodes[action.node_id]);
    return (
      <EpisodePlanCard
        question={question}
        actorId={actorId}
        glossaryIndex={glossaryIndex}
        onOpenNode={here ? onOpenNode : undefined}
        onSettled={cardsChanged}
      />
    );
  };
  const steeringTask = [...relatedTasks]
    .reverse()
    .find((task) => task.active && task.steer_visible && task.can_steer && task.steer_turn_id);
  const awaitingSteerReceipt = (Boolean(steeringTask) && submitting) || Boolean(stop);
  const queue = feed ? feed.queue : (upcoming.state?.queue ?? null);
  const queueable = Boolean(
    queue &&
    (relatedActive || queue.some((item) => item.kind === "handoff" || !item.refusal_reason)),
  );
  const apiBase = project ? `/api/projects/${encodeURIComponent(project.id)}` : "";
  const watchers = feed ? feed.watchers : hostWatchers;
  const onStopWatcher = feed
    ? (watcherId: string) =>
        void feed
          .stopWatcher(watcherId)
          .catch((error: unknown) => setSubmitError(errorMessage(error)))
    : hostStopWatcher;
  const watcherRows = useMemo(
    () => visibleChatWatchers(watchers, chatId, node, graphTarget),
    [chatId, node, watchers, graphTarget],
  );
  const pausedAttempt = resumablePausedChatTask(relatedTasks);
  const sessionId = resolvedChatSessionId(relatedTasks);
  const providerReady =
    feed || !project ? true : launchProviderReady(project, config, true, Boolean(sessionId));
  const chatTitle = feed?.thread.agent_name ?? (node?.title || conversationTitle || project!.name);
  const reportFailure = (run: () => Promise<unknown>) => {
    setSubmitError(null);
    run().catch((error: unknown) => setSubmitError(errorMessage(error)));
  };
  const onResumeTask = feed
    ? (task: AgentTask) => reportFailure(() => feed.control("resume", task))
    : hostResumeTask;
  const onRetryTask = feed
    ? (task: AgentTask) => reportFailure(() => feed.control("retry", task))
    : hostRetryTask;
  const onRefreshTask = async (taskId: string) => {
    if (hostRefreshTask) return hostRefreshTask(taskId);
    const task = await endpoints.loadTask(taskId);
    void feed?.refresh();
    return task;
  };
  const attachDisabled = composer.full || composer.preparing || submitting || awaitingSteerReceipt;
  const attachmentsUnready = composer.unready;
  const dictating = dictationState !== "idle" && dictationState !== "error";
  const kept = useKeptSpeech(draftKey);
  // The chat re-renders when the draft becomes sendable or empty, not per keystroke.
  const draftSendable = useComposerDraft(draft, (text) => Boolean(text.trim()));
  // macOS: the ring drains over the whole cap; the last 10 s also show the seconds.
  const dictationMsLeft =
    dictationState === "recording" && dictationDeadline !== null
      ? Math.max(0, dictationDeadline - dictationClock)
      : null;
  const dictationSecondsLeft =
    dictationMsLeft !== null && dictationMsLeft <= DICTATION_COUNTDOWN_MS
      ? Math.ceil(dictationMsLeft / 1000)
      : null;
  const dictationElapsed =
    dictationState === "recording" && dictationStartedAt !== null
      ? formatDictationElapsed(dictationClock - dictationStartedAt)
      : null;
  const dictationStatus =
    dictationElapsed !== null
      ? `Recording ${dictationElapsed}${dictationPiecesBusy ? " · transcribing…" : ""}`
      : dictationState === "preparing"
        ? "Downloading the macOS speech model…"
        : dictationState === "transcribing"
          ? `Transcribing with ${dictationService ?? "your service"}…`
          : dictationState === "recording" && dictationEngine === "apple_server"
            ? "Dictating with Apple's server recognizer"
            : null;
  useEffect(() => {
    skills.reset();
    // Settings supplies fresh conversation defaults; an open turn keeps its own.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset only when the chat changes, not when the picker object does
  }, [chatId, projectId]);

  useEffect(() => {
    const persist = () => {
      const text = draft.get();
      if (text) writeStorage(draftKey, text);
      else removeStorage(draftKey);
    };
    persist();
    return draft.subscribe(persist);
  }, [draft, draftKey]);

  useEffect(() => {
    if (!desktop) return;
    let disposed = false;
    const unlisten: Array<() => void> = [];
    void listenDesktopEvent<DictationResultEvent>("rcp://dictation-result", (payload) => {
      const span = liveDictationSpan(dictationSpanRef.current, payload.session_id);
      if (!span) return;
      draft.set((current) => {
        const next = replaceTextSpan(current, span, payload.text);
        span.end = next.end;
        skills.readMessage(next.value);
        return next.value;
      });
      window.requestAnimationFrame(() => {
        const active = dictationSpanRef.current;
        if (!active || active.sessionId !== payload.session_id) return;
        textareaRef.current?.setSelectionRange(active.end, active.end);
      });
    }).then((dispose) => (disposed ? dispose() : unlisten.push(dispose)));
    void listenDesktopEvent<DictationStateEvent>("rcp://dictation-state", (payload) => {
      // The recognizer holds the microphone until it reports the end, even after typing.
      if (payload.state === "stopped" || payload.state === "error")
        releaseMicrophone(payload.session_id);
      if (!liveDictationSpan(dictationSpanRef.current, payload.session_id)) return;
      if (payload.state === "preparing") setDictationState("preparing");
      if (payload.state === "recording") {
        setDictationState("recording");
        setDictationEngine(payload.engine ?? null);
        // The cap counts recording, not a first-use model download.
        if (dictationTimerRef.current === null) {
          dictationTimerRef.current = window.setTimeout(
            () => stopDictation(),
            DICTATION_SEGMENT_MS,
          );
          setDictationDeadline(Date.now() + DICTATION_SEGMENT_MS);
        }
      }
      if (payload.state === "error") {
        clearDictationTimer(dictationTimerRef);
        dictationSpanRef.current = null;
        setDictationState("error");
        setDictationError(payload.error || "Dictation stopped unexpectedly.");
      }
      if (payload.state === "stopped") {
        clearDictationTimer(dictationTimerRef);
        dictationSpanRef.current = null;
        setDictationState("idle");
      }
    }).then((dispose) => (disposed ? dispose() : unlisten.push(dispose)));
    return () => {
      disposed = true;
      unlisten.forEach((dispose) => dispose());
    };
    // The event bridge belongs to the native shell lifetime, not each draft render.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- subscribe once per native shell; handlers read refs
  }, [desktop]);

  useEffect(() => {
    if (dictationState !== "recording" || (dictationDeadline ?? dictationStartedAt) === null)
      return;
    setDictationClock(Date.now());
    const timer = window.setInterval(() => setDictationClock(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [dictationDeadline, dictationStartedAt, dictationState]);

  useEffect(() => {
    // A phone cuts the microphone when the app leaves the screen; treat that as Stop.
    const onVisibility = () => {
      if (document.visibilityState !== "hidden" || !networkDictationRef.current?.session?.recording)
        return;
      stopDictation();
      setDictationNote("Recording ended when the app went to the background.");
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- stopDictation reads refs
  }, []);

  useEffect(
    () => () => {
      stopDictation(true);
      releaseMicrophone();
    },
    // Dictation ends with the composer; both helpers read refs only.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- unmount-only cleanup
    [],
  );

  useEffect(() => {
    // Off the desktop only a connection can dictate; a macOS choice points to Settings.
    if (desktop) return;
    let cancelled = false;
    void loadServiceConnections().then(
      (settings) => {
        if (!cancelled)
          setDictationUnavailable(
            settings.dictation === "system" ? SYSTEM_DICTATION_ELSEWHERE : null,
          );
      },
      // Starting dictation reloads the choice and reports a failure there.
      () => {},
    );
    return () => {
      cancelled = true;
    };
  }, [desktop]);

  useEffect(() => {
    if (!sendAfterDictation || dictating) return;
    setSendAfterDictation(null);
    // A failed or kept transcription stays in the composer for the human to review.
    if (dictationState !== "idle" || keptSpeech(draftKey)) return;
    if (sendAfterDictation.target !== sendTarget(steeringTask, launchSettings(), relatedActive)) {
      setSubmitError(
        "The turn or its settings changed before dictation finished; nothing was sent.",
      );
      return;
    }
    void sendMessage();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fires on dictation end only
  }, [sendAfterDictation, dictating]);

  const updateMessage = (next: string) => {
    if (readOnly) return;
    if (!next) setSupersedes(null);
    draft.set(next);
    skills.readMessage(next);
    setSubmitError(null);
  };

  const retryTask = (task: AgentTask) => {
    if (!retryRefillsComposer(task)) return onRetryTask?.(task);
    if (dictating) stopDictation(true);
    updateMessage(typeof task.request.message === "string" ? task.request.message : "");
    setSupersedes({ chatId, operationId: task.operation_id });
    textareaRef.current?.focus();
  };

  const takeComposerRetry = useEffectEvent((task: AgentTask) => {
    onComposerRetryTaken?.();
    retryTask(task);
  });
  useEffect(() => {
    if (composerRetry && chatIdForTask(composerRetry) === chatId) takeComposerRetry(composerRetry);
  }, [composerRetry, chatId]);

  const releaseMicrophone = (sessionId?: string) => {
    const held = microphoneRef.current;
    if (!held || (sessionId !== undefined && held.sessionId !== sessionId)) return;
    microphoneRef.current = null;
    held.claim.release();
  };

  const failDictation = (sessionId: string, error: unknown) => {
    if (!liveDictationSpan(dictationSpanRef.current, sessionId)) return;
    clearDictationTimer(dictationTimerRef);
    dictationSpanRef.current = null;
    setDictationState("error");
    setDictationError(serviceConnectionFailure(error) ?? errorMessage(error));
  };

  /** Stop finishes the dictation; `detach` (typing, sending, leaving) keeps what is unwritten. */
  const stopDictation = (detach = false) => {
    const sessionId = dictationSpanRef.current?.sessionId;
    if (!sessionId) return;
    clearDictationTimer(dictationTimerRef);
    const network =
      networkDictationRef.current?.sessionId === sessionId ? networkDictationRef.current : null;
    const native =
      microphoneRef.current?.sessionId === sessionId &&
      microphoneRef.current.claim.holder === "native_dictation";
    if (network?.session) {
      if (detach) {
        // Nothing may land in the draft any more; the session keeps it for Insert,
        // which continues here if the draft is unchanged by then.
        const span = dictationSpanRef.current;
        network.resume = span ? { draft: draft.get(), at: span.end } : null;
        dictationSpanRef.current = null;
        setDictationState("idle");
        network.session.detach();
        return;
      }
      setDictationState("transcribing");
      network.session.finish();
      return;
    }
    if (detach || !native) {
      // Nothing from this session may land any more, including a late result.
      dictationSpanRef.current = null;
      setDictationState("idle");
    }
    if (network) {
      // The microphone was still opening; nothing was recorded.
      networkDictationRef.current = null;
      releaseMicrophone(sessionId);
      return;
    }
    if (!native) return;
    if (!detach) setDictationState("stopping");
    void stopDesktopDictation(sessionId, { finish: !detach }).catch((error) => {
      releaseMicrophone(sessionId);
      failDictation(sessionId, error);
    });
  };

  /** Insert dictated text at `at`, joined with one space; returns the new end. */
  /** Insert kept text at `at`; returns where the rest of that speech continues. */
  const insertDictatedText = (at: number, text: string) => {
    const current = draft.get();
    const addition = joinDictatedText(current.slice(0, at), text);
    const next = replaceTextSpan(current, { start: at, end: at }, addition);
    draft.set(next.value);
    skills.readMessage(next.value);
    window.requestAnimationFrame(() => textareaRef.current?.setSelectionRange(next.end, next.end));
    return { draft: next.value, at: next.end };
  };

  /** Append one piece's text to the live span; typing detached the span first. */
  const appendDictatedText = (sessionId: string, text: string) => {
    const span = liveDictationSpan(dictationSpanRef.current, sessionId);
    if (!span) return;
    // Read once: an updater may run twice, and must append once.
    const { end } = span;
    draft.set((current) => {
      const addition = joinDictatedText(current.slice(0, end), text);
      const next = replaceTextSpan(current, { start: end, end }, addition);
      span.end = next.end;
      skills.readMessage(next.value);
      return next.value;
    });
    window.requestAnimationFrame(() => {
      const active = liveDictationSpan(dictationSpanRef.current, sessionId);
      if (active) textareaRef.current?.setSelectionRange(active.end, active.end);
    });
  };

  const finishDictatedSpan = (sessionId: string) => {
    if (!liveDictationSpan(dictationSpanRef.current, sessionId)) return;
    dictationSpanRef.current = null;
    setDictationState("idle");
  };

  /** Keep speech that could not reach the draft; a live span is where it continues. */
  const keepSpeech = (
    sessionId: string,
    pieces: KeptPiece[],
    error: unknown,
    detachedResume: NetworkDictation["resume"] = null,
  ) => {
    const span = liveDictationSpan(dictationSpanRef.current, sessionId);
    addKeptSpeech(draftKey, {
      pieces,
      error,
      resume: span ? { draft: draft.get(), at: span.end } : (detachedResume ?? null),
    });
    if (!span) return;
    dictationSpanRef.current = null;
    setDictationState("idle");
  };

  const startNetworkDictation = async (sessionId: string, connection: ServiceConnection) => {
    const mimeType = chooseRecordingFormat(
      connection.formats,
      (type) => typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(type),
    );
    if (!mimeType)
      throw new Error(
        `${connection.label} accepts ${connection.formats.join(" or ")}, and this browser records neither.`,
      );
    const dictation: NetworkDictation = { sessionId, session: null };
    networkDictationRef.current = dictation;
    setDictationService(connection.label);
    microphoneRef.current = { sessionId, claim: claimMicrophone("network_dictation") };
    // The key check runs beside the permission prompt and ends before recording,
    // so it never takes a transcription slot an upload needs. A refused key would
    // fail every upload; any other listing failure says nothing and is ignored.
    const keyCheck = loadConnectionModels(connection.id).then(
      () => null,
      (error: unknown) => (serviceFailureCode(error) === "service_access_denied" ? error : null),
    );
    // A Stop or typing while permission is pending releases the claim, and this throws.
    const stream = await microphoneRef.current.claim.open();
    const refusal = await keyCheck;
    if (refusal) throw refusal;
    if (networkDictationRef.current !== dictation) return;
    // Leaving the screen while startup waited fired no Stop, since nothing was
    // recording yet; stop here rather than record in the background.
    if (document.visibilityState === "hidden") {
      stopDictation();
      setDictationNote("Recording ended when the app went to the background.");
      return;
    }
    const meter = createQuietMeter(stream);
    const session = new NetworkDictationSession({
      mimeType,
      createRecorder: () => new MediaRecorder(stream, { mimeType }),
      transcribe: async (audio) => (await transcribeAudio(connection.id, audio, mimeType)).text,
      isTransient: transientTranscriptionFailure,
      onText: (text) => appendDictatedText(sessionId, text),
      onBusy: setDictationPiecesBusy,
      onRecordingStopped: () => {
        if (networkDictationRef.current === dictation) clearDictationTimer(dictationTimerRef);
        meter.close();
        releaseMicrophone(sessionId);
      },
      // The session stays reachable until it settles, so typing after Stop still keeps speech.
      onSettled: () => {
        if (networkDictationRef.current === dictation) networkDictationRef.current = null;
        finishDictatedSpan(sessionId);
      },
      // Leaving the chat detached the session; its speech stays with this chat.
      onKept: (pieces, error) => {
        if (networkDictationRef.current === dictation) networkDictationRef.current = null;
        keepSpeech(sessionId, pieces, error, dictation.resume);
      },
    });
    dictation.session = session;
    try {
      session.start(Date.now());
    } catch (error) {
      meter.close();
      throw error;
    }
    setDictationStartedAt(Date.now());
    setDictationState("recording");
    dictationTimerRef.current = window.setInterval(() => {
      const now = Date.now();
      session.tick(now, meter.quietMs(now));
    }, 100);
  };

  /** Bring kept speech into the draft, transcribing kept audio with the current choice. */
  const insertKeptSpeech = async () => {
    const kept = keptSpeech(draftKey);
    if (!kept || readOnly || dictating) return;
    const at =
      kept.resume && kept.resume.draft === draft.get()
        ? kept.resume.at
        : (textareaRef.current?.selectionStart ?? draft.get().length);
    // A span like dictation's: typing, sending, or leaving detaches it, and the
    // result is kept again instead of landing at a stale offset.
    const sessionId = crypto.randomUUID();
    dictationSpanRef.current = { sessionId, start: at, end: at };
    setKeptSpeech(draftKey, null);
    setDictationError(null);
    setDictationNote(null);
    let result: Awaited<ReturnType<typeof resolveKeptPieces>>;
    try {
      let transcribe = async (_audio: Blob): Promise<string> => "";
      if (keptSpeechNeedsService(kept)) {
        setDictationState("transcribing");
        const settings = await loadServiceConnections();
        const connection = settings.connections.find((item) => item.id === settings.dictation);
        if (!connection)
          throw new Error("Choose a dictation connection in Space settings to retry.");
        setDictationService(connection.label);
        transcribe = async (audio) => {
          if (!connection.formats.includes(audio.type))
            throw new Error(`${connection.label} does not accept this recording's format.`);
          return (await transcribeAudio(connection.id, audio, audio.type)).text;
        };
      }
      result = await resolveKeptPieces(kept.pieces, transcribe);
    } catch (error) {
      result = { text: "", failure: { error, pending: kept.pieces } };
    }
    const resolved: KeptPiece[] = result.text
      ? [{ text: result.text, order: kept.pieces[0]?.order ?? 0 }]
      : [];
    const span = liveDictationSpan(dictationSpanRef.current, sessionId);
    let resume: KeptSpeech["resume"] = null;
    if (span) {
      dictationSpanRef.current = null;
      setDictationState("idle");
      resume = insertDictatedText(span.end, result.text);
    }
    const remaining = [...(span ? [] : resolved), ...(result.failure?.pending ?? [])];
    if (remaining.length)
      addKeptSpeech(draftKey, {
        pieces: remaining,
        error: result.failure?.error ?? null,
        // A tail left by a partial Retry continues after the text it inserted.
        resume,
      });
  };

  /** A new dictation or a send asks before kept speech is dropped. */
  const confirmDiscardKeptSpeech = (action: "dictate" | "send"): boolean => {
    if (!keptSpeech(draftKey)) return true;
    setDiscardPrompt(action);
    return false;
  };

  const toggleDictation = async () => {
    if (readOnly) return;
    if (dictating) {
      stopDictation();
      return;
    }
    if (!confirmDiscardKeptSpeech("dictate")) return;
    const textarea = textareaRef.current;
    const start = textarea?.selectionStart ?? draft.get().length;
    const sessionId = crypto.randomUUID();
    dictationSpanRef.current = { sessionId, start, end: start };
    setSubmitError(null);
    setDictationError(null);
    setDictationEngine(null);
    setDictationService(null);
    setDictationNote(null);
    setDictationDeadline(null);
    setDictationStartedAt(null);
    setDictationState("starting");
    try {
      // The choice is read now and kept, so a Settings change elsewhere cannot redirect this audio.
      const settings = await loadServiceConnections();
      if (!liveDictationSpan(dictationSpanRef.current, sessionId)) return;
      if (settings.dictation !== "system") {
        setDictationUnavailable(null);
        const connection = settings.connections.find((item) => item.id === settings.dictation);
        if (!connection) throw new Error("Your dictation service is no longer connected.");
        await startNetworkDictation(sessionId, connection);
        return;
      }
      if (!desktop) {
        setDictationUnavailable(SYSTEM_DICTATION_ELSEWHERE);
        throw new Error(SYSTEM_DICTATION_ELSEWHERE);
      }
      microphoneRef.current = { sessionId, claim: claimMicrophone("native_dictation") };
      await startDesktopDictation(sessionId);
      if (!liveDictationSpan(dictationSpanRef.current, sessionId)) return;
      setDictationState((current) => (current === "starting" ? "recording" : current));
    } catch (error) {
      if (networkDictationRef.current?.sessionId === sessionId) networkDictationRef.current = null;
      releaseMicrophone(sessionId);
      failDictation(sessionId, error);
    }
  };

  /** Steers the running turn with the composer's text and any comment threads, whose
   *  send key is the steer's message id. Resolves whether a receipt came back. */
  const steer = async (
    task: AgentTask,
    threads: { ids: readonly string[]; key: string } | null = null,
  ): Promise<boolean> => {
    const draftMessage = draft.get();
    const text = draftMessage.trim();
    // Files are staged for a new turn; a steer carries text only.
    if (!(text || threads)) return false;
    if (composer.staged || submitting || !task.steer_turn_id) {
      if (threads)
        setSubmitError(
          composer.staged
            ? "Remove the attached files to steer the running turn."
            : "Wait for the running turn to take a steer.",
        );
      return false;
    }
    if (dictating) stopDictation(true);
    scroll.current?.follow();
    const request: SteerRequest = {
      message_id: threads?.key ?? crypto.randomUUID(),
      attempt: task.attempt,
      expected_turn_id: task.steer_turn_id,
      message: text,
      ...(threads ? { comment_thread_ids: [...threads.ids] } : {}),
    };
    setSubmitError(null);
    setSubmitting(true);
    try {
      const receipt = await endpoints.steer(task.operation_id, request);
      setSteeringMessages((current) =>
        current.chatId !== chatId
          ? current
          : {
              chatId,
              messages: [
                ...current.messages.filter((entry) => entry.message_id !== receipt.message_id),
                receipt,
              ],
            },
      );
      // Typing and skill selection are fenced while the receipt is awaited, so
      // the delivered draft is exactly what is consumed.
      draft.set((current) => (current === draftMessage ? "" : current));
      skills.reset();
      void feed?.refresh();
      return true;
    } catch (error) {
      setSubmitError(
        `Steering receipt could not be read. Nothing was resent. ${error instanceof Error ? error.message : String(error)}`,
      );
      return false;
    } finally {
      setSubmitting(false);
    }
  };

  /** Everything a new turn launches with besides its text, for a dictated Send. */
  const launchSettings = () =>
    JSON.stringify([
      config,
      worktreeRepository,
      composer.turnFields(),
      skills.selection,
      worktree.chosen,
    ]);

  /** Sends the composer's text, with any comment threads under their send key: a steer
   *  while the turn takes one, else a new turn. Resolves whether it was sent. */
  const send = async (
    threads: { ids: readonly string[]; key: string } | null = null,
    queueing = false,
  ): Promise<boolean> => {
    const refuse = (reason: string) => {
      if (threads || queueing) setSubmitError(reason);
      return false;
    };
    if (stop) return refuse("The running turn is stopping.");
    if (threads ? !canSendThreads : readOnly) return refuse("This chat cannot send comments.");
    if (dictating) {
      if (threads) return refuse("Finish dictating before sending comments.");
      setSendAfterDictation({ target: sendTarget(steeringTask, launchSettings(), relatedActive) });
      if (dictationState !== "stopping" && dictationState !== "transcribing") stopDictation();
      return false;
    }
    if (steeringTask && !queueing) return steer(steeringTask, threads);
    return sendNewTurn(threads, queueing);
  };

  const sendNewTurn = async (
    threads: { ids: readonly string[]; key: string } | null = null,
    queueing = false,
  ): Promise<boolean> => {
    const refuse = (reason: string) => {
      if (threads || queueing) setSubmitError(reason);
      return false;
    };
    if ((relatedActive && !queueing) || submitting || repairingTaskId || reviewPending) {
      if (threads) setSubmitError("Wait for the running turn to finish.");
      return false;
    }
    if (worktree.chosen && !queueing && !worktree.state?.can_choose) {
      setSubmitError(
        worktree.error ??
          worktree.state?.unavailable_reason ??
          "Worktree eligibility has not been confirmed.",
      );
      return false;
    }
    const draftMessage = readOnly ? "" : draft.get();
    const text = draftMessage.trim();
    if (!(text || threads)) return false;
    if (attachmentsUnready) return refuse("Wait for the attachments to finish.");
    // Enter reaches here without the button, so a new turn checks readiness itself.
    if (!providerReady) return refuse("The agent is not ready to start a turn.");
    if (!confirmDiscardKeptSpeech("send")) return refuse("Sending was cancelled.");
    const queued = queueing
      ? JSON.stringify([
          text,
          composer.turnFields(),
          skills.selection,
          skills.providerSkillNames,
          [config.provider, config.model, config.reasoning],
        ])
      : null;
    const requestId =
      threads?.key ??
      (queued && queueFailure?.request === queued ? queueFailure.requestId : crypto.randomUUID());
    draft.set("");
    composer.closeMenu();
    return submitTurn({
      clientId: `pending-${requestId}`,
      requestId,
      text,
      draft: draftMessage,
      timestamp: new Date().toISOString(),
      mode: "work",
      attachments: readyAttachments,
      submission: {
        kind: surface,
        config,
        nodeId: node?.id ?? null,
        message: text,
        chatId,
        sessionId,
        supersedes: supersededId,
        attachments: composer.turnFields(),
        skills: skills.selection,
        providerSkillNames: skills.providerSkillNames,
        worktree: worktree.chosen && !queueing,
        worktreeRepository,
        ...(threads ? { commentThreadIds: threads.ids } : {}),
      },
      submittedOverride: configOverride,
      ...(queued ? { queue: true as const, queued } : {}),
    });
  };

  const startTurn = (
    submission: ConversationTurnSubmission,
    requestId: string = crypto.randomUUID(),
    queueing = false,
  ) =>
    feed
      ? feed.start(submission, requestId, queueing)
      : queueing
        ? queueThreadTurn(
            { thread_id: chatId },
            {
              message: submission.message.trim(),
              ...submission.attachments,
              ...(submission.commentThreadIds?.length
                ? { comment_thread_ids: [...submission.commentThreadIds] }
                : {}),
              ...skillInvocationFields(
                submission.skills ?? EMPTY_SKILL_SELECTION,
                submission.providerSkillNames ?? [],
              ),
              ...(configOverride?.chatId === chatId
                ? {
                    provider: submission.config.provider,
                    ...(submission.config.model ? { model: submission.config.model } : {}),
                    ...(submission.config.reasoning
                      ? { reasoning: submission.config.reasoning }
                      : {}),
                  }
                : {}),
            },
            requestId,
          ).then(() => upcoming.refresh())
        : startConversationTurn(onStartTask!, submission, requestId);

  /** Start a turn, or start it again after a failed send under the same key.
   *  Resolves whether it was admitted. */
  const submitTurn = async (turn: PendingChatTurn): Promise<boolean> => {
    if (turn.submission.commentThreadIds ? !canSendThreads : readOnly) return false;
    const pending = { ...turn, error: undefined };
    scroll.current?.follow();
    if (!turn.queue) setPendingTurn(pending);
    setSubmitError(null);
    setSubmitting(true);
    try {
      await startTurn(turn.submission, turn.requestId, Boolean(turn.queue));
      // The admitted turn now carries the pick, so the chat follows its history
      // again; a newer pick made while submitting stays.
      unsentTurns.delete(endpoints.scopeKey);
      setQueueFailure(null);
      setConfigOverride((current) => (current === turn.submittedOverride ? null : current));
      worktree.refresh();
      setPendingTurn((current) => (current?.clientId === turn.clientId ? null : current));
      draft.set((current) => (current === turn.draft ? "" : current));
      skills.reset();
      composer.clear();
      if (turn.submission.supersedes)
        setSupersedes((current) =>
          current?.operationId === turn.submission.supersedes ? null : current,
        );
      return true;
    } catch (error) {
      // A replaced backend admitted nothing and Reconnect is already offered, so
      // the text returns to the composer with no failed turn to resend. The
      // changed backend may already have unmounted this chat, so the stored draft
      // carries the text to the composer Reconnect reopens.
      if (error instanceof ApiError && error.code === "backend_instance_replaced") {
        if (!readStorage(draftKey)) writeStorage(draftKey, turn.draft);
        setPendingTurn((current) => (current?.clientId === turn.clientId ? null : current));
        draft.set((current) => (current ? current : turn.draft));
        return false;
      }
      // The request may never have reached the server, so the turn stays where it
      // was sent, with Resend under the same key: at most one turn is admitted.
      // The composer gets the text back too, so it survives leaving this chat.
      // fetch rejects a lost connection with a TypeError and the start timeout
      // with a TimeoutError; neither message tells the reader anything.
      const lost =
        error instanceof TypeError ||
        (error instanceof DOMException && error.name === "TimeoutError");
      const reason = lost ? "Not sent: the connection to RCP dropped." : errorMessage(error);
      if (turn.queued) setQueueFailure({ requestId: turn.requestId, request: turn.queued, reason });
      else unsentTurns.set(endpoints.scopeKey, { ...pending, error: reason });
      setPendingTurn((current) =>
        current?.clientId === turn.clientId ? { ...pending, error: reason } : current,
      );
      draft.set((current) => (current ? current : turn.draft));
      if (!readStorage(draftKey)) writeStorage(draftKey, turn.draft);
      return false;
    } finally {
      setSubmitting(false);
    }
  };

  const sendMessage = async (queueing = action === "Queue", deliver = send): Promise<boolean> => {
    const ids = dictating ? [] : commentDock.items.map((item) => item.thread_id);
    if (!ids.length) return deliver(null, queueing);
    const sent = await deliver({ ids, key: dockKeys.current.key(ids) }, queueing);
    if (sent) {
      dockKeys.current.done();
      commentDock.clear(ids);
    }
    return sent;
  };
  const replyIds = useMemo(
    () => [
      ...new Set(
        transcript
          .filter((line) => line.role === "agent" && line.text && !line.running)
          .map((line) => line.taskId),
      ),
    ],
    [transcript],
  );
  useLayoutEffect(() => {
    if (threadSender) threadSender.current = readOnly ? null : commentDock.dock;
  });
  useLayoutEffect(
    () => () => {
      if (threadSender) threadSender.current = null;
    },
    [threadSender],
  );
  const lastTurnLines = useMemo(
    () => new Map(transcript.map((line) => [line.taskId, line.lineId])),
    [transcript],
  );
  const sendSaved: DockThreads = (saved) => {
    const ids = saved.map((thread) => thread.thread_id);
    void send({ ids, key: dockKeys.current.key(ids) }).then((sent) => {
      if (sent) dockKeys.current.done();
    });
  };
  const chatCommentScope = useMemo<CommentScope>(
    () => ({ kind: "project", projectId: projectId ?? "", threadId: chatId }),
    [projectId, chatId],
  );
  const commentScope = feed?.commentScope ?? chatCommentScope;
  const inlineArtifactComments = useInlineArtifactComments({
    scope: commentScope,
    canPost: canSendThreads,
    dock: readOnly ? (allowArtifactComments ? sendSaved : null) : commentDock.dock,
  });
  const replyComments = useReplyComments({
    scope: commentScope,
    chatId,
    replyIds,
    linesRef: chatLinesRef,
    canPost: !readOnly,
    dock: readOnly ? null : commentDock.dock,
    artifacts: inlineArtifactComments.artifacts,
  });

  const integrateWorktree = async (option: WorktreeIntegrationOption) => {
    if (
      readOnly ||
      relatedActive ||
      pausedAttempt ||
      submitting ||
      reviewPending ||
      repairingTaskId
    )
      return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await startTurn({
        kind: surface,
        config,
        nodeId: node?.id ?? null,
        message: option.label,
        chatId,
        sessionId,
        worktreeIntegration: option.id,
      });
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : String(error));
    } finally {
      setSubmitting(false);
      worktree.refresh();
    }
  };

  const repairGraphUpdate = async (taskId: string, action: GraphUpdateRecovery = "repair") => {
    if (readOnly || repairingTaskId) return;
    setRepairingTaskId(taskId);
    setRepairErrors((current) => withoutMapKey(current, taskId));
    try {
      await onRepairGraphUpdate?.(taskId, action);
    } catch (error) {
      setRepairErrors((current) =>
        withMapValue(current, taskId, error instanceof Error ? error.message : String(error)),
      );
    } finally {
      setRepairingTaskId(null);
    }
  };

  const artifacts = useAgentWindowArtifacts({
    endpoints,
    chatId,
    relatedTasks,
    transcript,
    inlineComments: inlineArtifactComments.handlers,
    onRefreshTask,
    readOnly,
    allowArtifactComments,
    repositories: project?.repositories ?? [],
  });

  const watcherToggle = watcherRows.length > 0 && (
    <button
      className={`chat-watcher-count${watchersOpen ? " is-open" : ""}`}
      type="button"
      aria-expanded={watchersOpen}
      aria-label={`${watcherRows.length} active watcher${watcherRows.length === 1 ? "" : "s"}`}
      onClick={() => setWatchersOpen((open) => !open)}
    >
      <RadioTower size={12} /> {watcherRows.length}
    </button>
  );

  const effectiveModel =
    profile && config.provider === profile.provider ? profile.effective_model : "";
  const hasHistory = relatedTasks.length > 0 || Boolean(historyMessages?.length);
  const renameTitle = feed
    ? feed.rename
    : header === undefined && project && !node && !fixedConversation && !readOnly && hasHistory
      ? (title: string) => setChatTitle(apiBase, chatId, title).then(() => setRenamedTitle(title))
      : null;
  const pickConfig = (next: AgentRunConfig) => {
    if (feed) reportFailure(() => feed.pickAgent(next));
    else setConfigOverride({ chatId, config: next });
  };

  const archiveThread = async (value: boolean) => {
    await setChatArchived(apiBase, chatId, value);
    if (feed) onThreadChanged?.({ ...feed.thread, archived: value });
  };

  const headerChips = (
    <>
      {headerTools}
      {(feed || upcoming.state) && (
        <SchedulesPopover
          threadId={chatId}
          schedules={feed?.schedules ?? upcoming.state?.schedules ?? []}
          onChanged={() => void (feed ? feed.refreshUpcoming() : upcoming.refresh())}
        />
      )}
      <AgentStatusStrip tasks={statusTasks} />
      {watcherToggle}
      {replyComments.headerButton}
      {artifactsPane && (
        <button
          className={`chat-watcher-count${artifactsOpen ? " is-open" : ""}`}
          type="button"
          aria-expanded={artifactsOpen}
          aria-label="Artifacts"
          title="Artifacts"
          onClick={() => setArtifactsOpen((open) => !open)}
        >
          <Files size={12} /> {chatArtifacts.length}
        </button>
      )}
    </>
  );
  const headerMenu = (
    <ChatHeaderMenu
      link={feed || !project ? undefined : { projectId: project.id, chatId, graphTarget }}
      actions={[
        ...(renameTitle ? [{ label: "Rename", onSelect: () => setRenaming(true) }] : []),
        ...headerActions,
        ...(feed && project
          ? [
              {
                label: isArchived ? "Restore" : "Archive",
                disabled: !isArchived && relatedActive,
                onSelect: () => reportFailure(() => archiveThread(!isArchived)),
              },
            ]
          : []),
        ...(!fixedConversation && !readOnly && onNewSession
          ? [{ label: "New session", onSelect: onNewSession }]
          : []),
      ]}
    >
      {headerMenuItems}
      {feed && project && !isArchived && (
        <ChatBranchPicker
          projectId={project.id}
          chatId={chatId}
          graphTarget={graphTarget}
          disabled={relatedActive || !sessionId}
        />
      )}
      {feed ? (
        <div className="chat-browser-control">
          <BrowserToggle
            checked={feed.thread.browser_requested}
            disabled={readOnly}
            onChange={(value) => reportFailure(() => feed.setBrowser(value))}
          />
        </div>
      ) : (
        project && (
          <ChatBrowserControl
            key={`${project.id}:${chatId}`}
            apiBase={apiBase}
            chatId={chatId}
            machine={config.run_on}
            disabled={readOnly}
          />
        )
      )}
      {configSource && (
        <AgentPickRow
          source={configSource}
          value={config}
          effectiveModel={effectiveModel}
          workLikeCapable={profile?.work_like_capable}
          disabled={readOnly || pendingFork}
          onChange={pickConfig}
          onShown={feed?.reloadPicker}
        />
      )}
    </ChatHeaderMenu>
  );

  const latest = relatedTasks.at(-1) ?? null;
  const liveTask =
    [...relatedTasks]
      .reverse()
      .find(
        (task) =>
          isActiveTask(task) && !(pendingTurn?.error && pendingTurn.clientId === task.operation_id),
      ) ?? null;
  const pausedTask = latest && !latest.active && latest.can_resume ? latest : null;
  const failedTask =
    latest && !latest.active && latest.can_retry && !latest.can_resume ? latest : null;
  const stoppedTask =
    failedTask &&
    !transcript.some((line) => line.role === "error" && line.taskId === failedTask.operation_id)
      ? failedTask
      : null;
  const dockedCount = commentDock.items.length;
  const steerable = steeringTask && !(composer.staged && queue) ? steeringTask : null;
  const action: "Steer" | "Queue" | "Send" = steerable ? "Steer" : queueable ? "Queue" : "Send";
  const sendBlocked =
    action !== "Queue" &&
    (steerable
      ? Boolean(composer.staged)
      : attachmentsUnready ||
        relatedActive ||
        Boolean(repairingTaskId) ||
        reviewPending ||
        !providerReady);
  const sendReason = submitting
    ? "Sending"
    : sendBlocked
      ? steerable
        ? "Attachments wait until this turn ends"
        : relatedActive
          ? "Wait until this turn ends"
          : attachmentsUnready
            ? "Attachments are still uploading"
            : undefined
      : !(draftSendable || dictating || dockedCount)
        ? "Write a message"
        : undefined;
  const sendDisabled =
    submitting || Boolean(stop) || sendBlocked || !(draftSendable || dictating || dockedCount);
  const stopSends =
    !submitting &&
    !(queue?.length ?? 0) &&
    !attachmentsUnready &&
    providerReady &&
    !dictating &&
    !reviewPending &&
    (draftSendable || dockedCount > 0);
  const stopTurn = async (task: AgentTask) => {
    const request = {
      scope: endpoints.scopeKey,
      operationId: task.operation_id,
      attempt: task.attempt,
      sends: stopSends,
      message: draft.get(),
      sessionId,
    };
    setStop(request);
    setSubmitError(null);
    try {
      if (feed) await feed.control("pause", task);
      else {
        await endpoints.control(task.operation_id, "pause", crypto.randomUUID());
        await onRefreshTask(task.operation_id);
      }
      setStop((current) => (current === request ? { ...request, result: true } : current));
    } catch (error) {
      setStop((current) =>
        current === request ? { ...request, result: new Error(errorMessage(error)) } : current,
      );
    }
  };
  const finishStop = useEffectEvent(() => {
    if (!stop) return;
    const same =
      stop.scope === endpoints.scopeKey &&
      stop.operationId === latest?.operation_id &&
      stop.attempt === latest.attempt;
    if (same && (!stop.result || (stop.result === true && latest.active))) return;
    setStop(null);
    if (same && stop.result instanceof Error) setSubmitError(stop.result.message);
    if (
      same &&
      stop.result === true &&
      latest.paused &&
      (!stop.sessionId || stop.sessionId === sessionId) &&
      !readOnly &&
      stop.sends &&
      draft.get() === stop.message
    )
      void sendMessage(false, sendNewTurn);
  });
  useEffect(() => {
    finishStop();
  }, [stop, latest, endpoints.scopeKey]);
  const removeQueued = async (item: QueuedMessage, dismissed: boolean) => {
    setSubmitError(null);
    try {
      if (feed) await feed.removeQueued(item);
      else {
        await removeQueuedTurn({ thread_id: chatId }, item.queue_id);
        await upcoming.refresh();
      }
    } catch (error) {
      setSubmitError(errorMessage(error));
      throw error;
    }
    if (dismissed) draft.set((current) => [item.message, current].filter(Boolean).join("\n\n"));
  };
  const firstLineOfTurn = new Map<string, string>();
  for (const line of shownLines)
    if (!firstLineOfTurn.has(line.taskId)) firstLineOfTurn.set(line.taskId, line.lineId);
  const firstTurnId = firstLineOfTurn.keys().next().value;
  const welcome =
    feed &&
    feed.messages !== null &&
    !feed.loadError &&
    !transcript.length &&
    !questionState.questions.length &&
    !pendingTurn &&
    !latest
      ? feed.welcome
      : null;
  const voiceHere = voice !== null && voice.phase !== "idle" && voice.thread?.thread_id === chatId;
  const voiceBusy = voice !== null && voice.phase !== "idle";
  const shownTitle = feed ? feed.thread.agent_name : (renamedTitle ?? chatTitle);
  const heading =
    header ??
    (renaming && renameTitle ? (
      <AgentRename
        title={shownTitle}
        onDone={(title) => {
          const fromField = document.activeElement?.closest(".agent-row-rename");
          setRenaming(false);
          if (fromField)
            requestAnimationFrame(() =>
              dockRef.current
                ?.querySelector<HTMLButtonElement>(".chat-header-menu > button")
                ?.focus(),
            );
          if (title && title !== shownTitle) reportFailure(() => renameTitle(title));
        }}
      />
    ) : (
      <strong title={feed?.thread.project_name ?? undefined}>{shownTitle}</strong>
    ));

  return (
    <div
      ref={dockRef}
      className={`chat-dock ${presentation}${artifactsPane && artifactsOpen ? " artifacts-open" : ""}`}
      role={presentation === "floating" ? "dialog" : "region"}
      aria-modal="false"
      aria-label={
        feed
          ? feed.thread.agent_name
          : node || conversationTitle
            ? `Chat about ${chatTitle}`
            : "Project chat"
      }
    >
      <AgentHeader
        leading={leading}
        title={heading}
        chips={headerChips}
        menu={headerMenu}
        state={headerState}
        dragHandle={presentation === "floating"}
        trailing={
          presentation === "floating" && (
            <button
              className="icon-button"
              type="button"
              onClick={onClose}
              aria-label="Minimize chat; background work will continue"
            >
              <X size={16} />
            </button>
          )
        }
      />
      {artifactsPane && artifactsOpen && (
        <ChatArtifactsPane
          projectId={projectId!}
          artifacts={chatArtifacts}
          onClose={() => setArtifactsOpen(false)}
        />
      )}
      {watcherRows.length > 0 && watchersOpen && (
        <section className="chat-watchers" aria-label="Watchers">
          {watcherRows.map((watcher) => {
            const external = isExternalWatcherRecord(watcher);
            const observedAt = watcherLastObservedAt(watcher);
            return (
              <div className={`chat-watcher-row ${watcher.status}`} key={watcher.watcher_id}>
                {external ? (
                  <ExternalJobRow
                    apiBase={feed ? threadPath(feed.thread.thread_id) : apiBase}
                    watcher={watcher}
                  />
                ) : (
                  <>
                    <strong>{graphConditionLabel(watcher.condition)}</strong>
                    <time dateTime={observedAt ?? undefined}>
                      {observedAt
                        ? `Evaluated ${new Date(observedAt).toLocaleString()}`
                        : "Not evaluated yet"}
                    </time>
                  </>
                )}
                {!readOnly && onStopWatcher && watcher.can_stop_watching && (
                  <button
                    className="button compact"
                    type="button"
                    onClick={() => onStopWatcher(watcher.watcher_id)}
                  >
                    Stop watching
                  </button>
                )}
              </div>
            );
          })}
        </section>
      )}
      <AgentWindowLines
        scrollKey={endpoints.scopeKey}
        linesRef={chatLinesRef}
        transcript={transcript}
        questions={questionState.questions}
        scroll={scroll}
      >
        {feed && feed.messages === null && !feed.loadError && (
          <div className="agent-chat-view-line meta" role="status">
            <LoaderCircle className="spin" size={14} />
          </div>
        )}
        {feed?.loadError && (
          <div className="agent-chat-view-line error" role="alert">
            {feed.loadError}
            <button
              type="button"
              className="icon-button chat-error-retry"
              aria-label="Retry"
              title="Retry"
              onClick={() => void feed.refresh()}
            >
              <RotateCcw size={14} />
            </button>
          </div>
        )}
        {welcome && (
          <AgentThreadWelcome
            welcome={welcome}
            onPrompt={(text) => {
              updateMessage(text);
              textareaRef.current?.focus();
            }}
          />
        )}
        {origin && project && <ChatOriginLink projectId={project.id} origin={origin} />}
        <AgentWindowTranscript
          transcript={shownLines}
          replyLinkGraphTarget={
            feed
              ? undefined
              : graphTarget.kind === "branch"
                ? `branch:${graphTarget.branch_id}`
                : "main"
          }
          tasks={relatedTasks}
          questions={questionState.questions}
          endpoints={endpoints}
          artifacts={artifacts}
          lineTrail={
            turnTrail &&
            ((line) => lastTurnLines.get(line.taskId) === line.lineId && turnTrail(line.taskId))
          }
          compactionByLine={compactionByLine}
          continuedTaskIds={continuedTaskIds}
          latestGraphReceiptLineIds={latestGraphReceiptLineIds}
          renderQuestion={(question) =>
            questionApiBase ? (
              <QuestionCard
                question={question}
                apiBase={questionApiBase}
                onResolved={questionState.refresh}
              />
            ) : null
          }
          renderPlanCard={renderPlanCard}
          shownElsewhere={dockedPlans}
          renderCards={(entry) => (
            <ApprovalCardStack
              stackKey={`${chatId}:${entry.key}`}
              cards={entry.asked.map((question) =>
                askedStackCard(question, feed?.thread.project_name ?? null),
              )}
              onDecided={cards.markDecided}
              onResolved={cards.markResolved}
              onAnswered={cards.markAnswered}
              onChanged={cardsChanged}
            />
          )}
          lineClassName={(line) =>
            line.trigger === "schedule" && line.role === "human" ? "agent schedule-wake" : line.role
          }
          humanBody={(line) => (line.trigger === "schedule" ? <></> : undefined)}
          agentBody={(line) => {
            const report = handoffReport(line);
            if (!report) return undefined;
            return (
              <HandoffResult
                report={report}
                agentName={feed?.handoffAgentName(report.threadId) ?? "Project agent"}
              />
            );
          }}
          triggerChip={watcherTrigger}
          lineLead={(line) => (
            <>
              {firstLineOfTurn.get(line.taskId) === line.lineId && (
                <FreshSessionMarker
                  task={relatedTasks.find((task) => task.operation_id === line.taskId)}
                  hasEarlierTurn={line.taskId !== firstTurnId}
                />
              )}
              {line.trigger === "schedule" && line.role === "human" && (
                <span className="chat-turn-trigger schedule" title={line.text}>
                  <CalendarClock size={11} aria-hidden="true" />
                  {localClock(new Date(line.timestamp))}
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
          nodes={nodes}
          glossaryIndex={glossaryIndex}
          onOpenNode={onOpenNode}
          pendingTurn={pendingTurn}
          onResend={() => pendingTurn && void submitTurn(pendingTurn)}
          submitting={submitting}
          readOnly={readOnly}
          graphChangesDisabled={graphChangesDisabled}
          reviewPending={reviewPending}
          repairingTaskId={repairingTaskId}
          repairErrors={repairErrors}
          onResumeTask={onResumeTask}
          onRetryTask={retryTask}
          onInspectTask={onInspectTask}
          onOpenInbox={onOpenInbox}
          onRepairGraphUpdate={repairGraphUpdate}
        />
        {liveTask && (
          <div className="agent-chat-view-line agent">
            {!feed && liveLine && watcherTrigger(liveLine)}
            <InlineTaskProgress
              task={liveTask}
              control={
                liveTask.can_pause &&
                !readOnly && (
                  <button
                    type="button"
                    className="button compact secondary"
                    disabled={submitting || Boolean(stop) || liveTask.pausing}
                    onClick={() => void stopTurn(liveTask)}
                  >
                    <Square size={11} /> {stopSends ? "Stop and send" : "Stop"}
                  </button>
                )
              }
            />
          </div>
        )}
        {(pausedTask || stoppedTask) && (
          <div className="chat-task-inline paused" role="status">
            <span>{pausedTask ? pausedTaskText(pausedTask) : stoppedTask!.status_message}</span>
            {!readOnly && !(pausedTask && stoppedByMember(pausedTask)) && (
              <button
                type="button"
                className="button compact primary"
                onClick={() =>
                  pausedTask ? onResumeTask?.(pausedTask) : onRetryTask?.(stoppedTask!)
                }
              >
                <Play size={12} /> Continue
              </button>
            )}
          </div>
        )}
        {questionState.error && (
          <div className="agent-chat-view-line error" role="alert">
            {questionState.error}
          </div>
        )}
        {questionState.questions
          .filter(
            (question) =>
              questionIsOpen(question) && !question.card && !question.withdrawn_readonly,
          )
          .map((question) =>
            questionApiBase ? (
              <QuestionCard
                key={question.question_id}
                question={question}
                apiBase={questionApiBase}
                onResolved={questionState.refresh}
                continueChat={
                  !readOnly && !questionEpisodeId && question.state === "parked" && !relatedActive
                }
              />
            ) : null,
          )}
        {submitError && (
          <div className="agent-chat-view-line error" role="alert">
            {submitError}
          </div>
        )}
      </AgentWindowLines>
      {(dockCards.length > 0 || cards.unavailable) && (
        <div className="approval-dock">
          {cards.unavailable && <AgentCardsUnavailable />}
          <ApprovalCardStack
            stackKey={`${chatId}:dock`}
            cards={dockCards}
            onDecided={cards.markDecided}
            onResolved={cards.markResolved}
            onAnswered={cards.markAnswered}
            onChanged={cardsChanged}
            renderPlanCard={renderPlanCard}
          />
        </div>
      )}
      {queue && <ThreadQueue items={queue} onRemove={removeQueued} />}
      {isArchived ? (
        <div className="chat-composer" role="status">
          <button
            className="button compact"
            type="button"
            onClick={feed ? () => reportFailure(() => archiveThread(false)) : onRestore}
          >
            Restore
          </button>
        </div>
      ) : (
        readOnly && readOnlyNotice
      )}
      {!readOnly && (
        <ChatComposer
          hint={
            !feed &&
            project && (
              <ProviderSwitchHint
                project={project}
                config={config}
                tasks={relatedTasks}
                sessionId={sessionId}
              />
            )
          }
          composer={composer}
          docked={commentDock}
          skills={skills.props}
          addDisabled={attachDisabled}
          addOptionCount={optionCount}
          addOptions={
            project && (
              <WorktreeChooser
                state={worktree.state}
                chosen={worktree.chosen}
                disabled={Boolean(
                  relatedActive || pausedAttempt || submitting || reviewPending || repairingTaskId,
                )}
                onChoose={worktree.choose}
                repositories={worktreeRepositories}
                repository={worktreeRepository}
                onRepository={(alias) => setWorktreeRepositoryPick({ chatId, alias })}
              />
            )
          }
          field={
            <ComposerTextarea
              draft={draft}
              ref={textareaRef}
              aria-label="Message"
              disabled={awaitingSteerReceipt}
              onChange={(event) => {
                setSendAfterDictation(null);
                if (dictating) stopDictation(true);
                updateMessage(event.target.value);
              }}
              onPaste={composer.paste}
              onKeyDown={(event) => {
                if (skills.handleKeyDown(event)) return;
                if (arrowScrollsTranscript(event, chatLinesRef.current)) return;
                if (returnSends(event) && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  if (!sendDisabled || dictating) void sendMessage();
                }
              }}
            />
          }
          tools={
            project && (
              <WorktreeControls
                key={`${project.id}:${chatId}`}
                state={worktree.state}
                error={worktree.error}
                disabled={Boolean(
                  relatedActive || pausedAttempt || submitting || reviewPending || repairingTaskId,
                )}
                onIntegrate={integrateWorktree}
                onRemove={worktree.remove}
                onPreviewRemove={worktree.previewRemoval}
                onRefresh={worktree.refresh}
              />
            )
          }
          actions={
            <>
              {voice ? (
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
                  onClick={() => (voiceBusy ? voice.end() : feed && void voice.open(feed.thread))}
                >
                  {voice.phase === "starting" && voiceHere ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : voiceBusy ? (
                    <MicOff size={16} />
                  ) : (
                    <Mic size={16} />
                  )}
                </button>
              ) : (
                <button
                  className={`icon-button chat-dictation-button${
                    dictating && dictationState !== "transcribing" ? " recording" : ""
                  }${dictationMsLeft !== null ? " counting" : ""}${
                    dictationSecondsLeft !== null ? " ending" : ""
                  }`}
                  style={
                    dictationMsLeft !== null
                      ? ({
                          "--dictation-left": dictationMsLeft / DICTATION_SEGMENT_MS,
                        } as React.CSSProperties)
                      : undefined
                  }
                  type="button"
                  aria-label={dictating ? "Stop dictation" : "Start dictation"}
                  aria-pressed={dictating}
                  aria-disabled={dictationUnavailable ? true : undefined}
                  title={
                    dictationUnavailable ||
                    dictationError ||
                    (dictating ? "Stop dictation" : "Dictate")
                  }
                  disabled={submitting || Boolean(stop) || dictationState === "transcribing"}
                  onClick={() => void toggleDictation()}
                >
                  {dictationState === "transcribing" ? (
                    <LoaderCircle className="spin" size={16} />
                  ) : dictating ? (
                    <MicOff size={16} />
                  ) : (
                    <Mic size={16} />
                  )}
                </button>
              )}
              {dictationSecondsLeft !== null && (
                <span className="chat-dictation-countdown" aria-live="polite">
                  {dictationSecondsLeft}s
                </span>
              )}
              {queue && action === "Steer" && (
                <button
                  className="icon-button chat-queue-button"
                  type="button"
                  aria-label="Queue"
                  title={draftSendable || dockedCount ? "Queue" : "Write a message"}
                  disabled={submitting || !(draftSendable || dockedCount)}
                  onClick={() => void sendMessage(true)}
                >
                  <ListPlus size={16} />
                </button>
              )}
              <button
                className="icon-button primary chat-send-button"
                type="button"
                disabled={sendDisabled}
                title={sendReason ?? (action === "Send" ? undefined : action)}
                onClick={() => void sendMessage()}
                aria-label={
                  action === "Steer" ? (steeringTask?.steer_action_label ?? "Steer") : action
                }
              >
                {submitting ? (
                  <LoaderCircle className="spin" size={16} />
                ) : action === "Queue" ? (
                  <ListPlus size={16} />
                ) : (
                  <Send size={16} />
                )}
              </button>
            </>
          }
          notice={
            queueFailure ? (
              <span role="alert">{queueFailure.reason}</span>
            ) : discardPrompt ? (
              <span className="chat-dictation-error" role="alert">
                Discard the kept dictation?
                <button
                  className="button compact secondary"
                  type="button"
                  onClick={() => {
                    const action = discardPrompt;
                    setDiscardPrompt(null);
                    setKeptSpeech(draftKey, null);
                    void (action === "send" ? sendMessage() : toggleDictation());
                  }}
                >
                  Discard
                </button>
                <button
                  className="button compact secondary"
                  type="button"
                  onClick={() => setDiscardPrompt(null)}
                >
                  Keep
                </button>
              </span>
            ) : kept && !dictating ? (
              <span className="chat-dictation-error" role="alert">
                {kept.error
                  ? (serviceConnectionFailure(kept.error) ?? errorMessage(kept.error))
                  : "Dictation stopped when you typed; its text is kept."}
                <button
                  className="button compact secondary"
                  type="button"
                  onClick={() => void insertKeptSpeech()}
                >
                  {keptSpeechNeedsService(kept) ? "Retry" : "Insert"}
                </button>
                <button
                  className="button compact secondary"
                  type="button"
                  onClick={() => setKeptSpeech(draftKey, null)}
                >
                  Discard
                </button>
              </span>
            ) : dictationError ? (
              <span className="chat-dictation-error" role="alert">
                {dictationError}
              </span>
            ) : dictationStatus || dictationNote ? (
              <span className="chat-dictation-status" role="status">
                {dictationStatus ?? dictationNote}
              </span>
            ) : null
          }
        />
      )}
      {replyComments.layers}
      {inlineArtifactComments.layer}
    </div>
  );
}

/** The composer's text field: the only part of the chat a keystroke re-renders. */
function ComposerTextarea({
  draft,
  ...props
}: { draft: ComposerDraft } & Omit<React.ComponentProps<"textarea">, "value">) {
  const value = useComposerDraft(draft);
  return <ComposerField {...props} value={value} />;
}

function clearDictationTimer(ref: React.MutableRefObject<number | null>): void {
  // One ref holds the macOS stop timeout or the network piece interval.
  if (ref.current !== null) {
    window.clearTimeout(ref.current);
    window.clearInterval(ref.current);
  }
  ref.current = null;
}

function formatDictationElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/** A server error, timeout, or lost connection; a refusal or rejection never retries. */
function transientTranscriptionFailure(error: unknown): boolean {
  const code = serviceFailureCode(error);
  if (code) return code === "transcription_upstream_failed" || code === "transcription_busy";
  return !(error instanceof ApiError) || error.status >= 500;
}

function serviceFailureCode(error: unknown): string | null {
  const code = (error as { detail?: { code?: unknown } } | null)?.detail?.code;
  return typeof code === "string" ? code : null;
}

function readStorage(key: string | null): string | null {
  if (!key) return null;
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string | null, value: string): void {
  if (!key) return;
  try {
    localStorage.setItem(key, value);
  } catch {}
}

function removeStorage(key: string | null): void {
  if (!key) return;
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
