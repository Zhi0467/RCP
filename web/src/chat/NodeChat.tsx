import { ChatOriginLink } from "./ChatBranchPicker";
import { ProviderSwitchHint } from "./ProviderSwitchHint";
import { ChatHeaderMenu, type ChatHeaderAction } from "./ChatHeaderMenu";
import type { ChatAgentSetup, ChatOrigin } from "../core/types";
import { turnOutcomeChip } from "./turnOutcome";
import { memberDraftKey } from "../core/draftStorage";
import { MAIN_GRAPH } from "../core/graphTarget";
import { ReferenceChip } from "../core/ReferenceChip";
import {
  ComposerAddPicker,
  ComposerAttachmentChips,
  formatBytes,
  useComposerAttachments,
  type InsertReferenceText,
} from "./composerAttachments";
import { ArtifactCommentQuotes, artifactCommentLead } from "./ArtifactCommentQuotes";
import { referenceDraftKey, sourceReference, type DraftReference } from "../core/projectReferences";
import { QuestionCard } from "./QuestionCard";
import { useQuestions } from "./useQuestions";
import { questionIsOpen, questionTranscript } from "./questions";
import { ExternalJobRow } from "../experiments/ExternalJobRow";
import {
  TriangleAlert,
  ChevronUp,
  Download,
  ExternalLink,
  File,
  History,
  Inbox,
  LoaderCircle,
  MessageCircle,
  MessageCirclePlus,
  Mic,
  MicOff,
  Play,
  RadioTower,
  RotateCcw,
  Send,
  SlidersHorizontal,
  X,
} from "lucide-react";
import {
  Fragment,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
  api,
  ApiError,
  loadServiceConnections,
  steerChatTurn,
  loadConnectionModels,
  transcribeAudio,
} from "../core/api";
import {
  artifactUrl,
  chatTasksMissingFromHistory,
  isActiveTask,
  resolvedChatSessionId,
  orderTranscriptLines,
  isNewestOfTurnClass,
  reconcileChatHistoryArtifacts,
  reconstructTaskTranscript,
  relatedChatTasks,
  resumablePausedChatTask,
  taskArtifacts,
  taskKindLabel,
  type TaskTranscriptLine,
  versionedArtifactContentUrl,
} from "../agents/agentTasks";
import {
  chatDraftStorageKey,
  chatModeStorageKey,
  isConversationModeShortcut,
  latestPersistedChatConfig,
  originRunConfig,
  latestPersistedConversationMode,
  parseConversationMode,
  providerSwitchStartsFreshSession,
  startConversationTurn,
  toggleConversationMode,
  type ConversationTurnSubmission,
  type StartConversationTask,
} from "./chatWorkspace";
import { chatFollowsBottom, chatReaderMovedUp } from "./chatLayout";
import {
  openArtifact as openArtifactPanel,
  openRepositoryFile as openRepositoryFilePanel,
} from "../artifacts/artifactViewerModel";
import { MarkdownAnswer } from "../core/chatMarkdown";
import { returnSends } from "../ui/composerKeys";
import {
  InlineArtifact,
  InlineArtifactMissing,
  type InlineArtifactSelectionEvent,
} from "../artifacts/InlineArtifact";
import { turnArtifactFor, inlineArtifactIds, isInlineViewable } from "../artifacts/inlineArtifacts";
import {
  assembleChatTurn,
  chatAnnotationComposerPosition,
  chatAnnotationTextControlSelection,
  chatAnnotationViewportMetrics,
  chatSelectionCommentPosition,
  MAX_CHAT_ANNOTATIONS,
  MAX_CHAT_ANNOTATION_COMMENT_LENGTH,
  MAX_CHAT_ANNOTATION_TEXT_LENGTH,
  annotatableAnswerSelectionRange,
  parseStagedChatAnnotations,
  replaceTextSpan,
  stagedArtifactContext,
  stagedChatAnnotationsAreComplete,
  type ChatAnnotationAnchor,
  type ChatAnnotationComposerPosition,
  type ChatAnnotationViewportMetrics,
  type StagedArtifactTarget,
  type StagedChatAnnotation,
} from "./chatInput";
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
  downloadDesktopArtifact,
  type DictationResultEvent,
  type DictationStateEvent,
  isDesktopRuntime,
  listenDesktopEvent,
  openDesktopArtifactPdf,
  startDesktopDictation,
  stopDesktopDictation,
} from "../core/desktopRuntime";
import { resolveRepositoryFileHref, turnArtifactLocation } from "../core/repositoryFileLinks";
import type {
  AgentArtifactDescriptor,
  ArtifactEditOutcome,
  AgentRunConfig,
  AgentTask,
  ChatMessage,
  TurnOutcome,
  ChatAttachmentDescriptor,
  ConversationMode,
  GraphNode,
  GraphTargetRef,
  GraphUpdateRecovery,
  GraphUpdateResult,
  ProjectSnapshot,
  ServiceConnection,
  WatcherRecord,
  WorktreeIntegrationOption,
} from "../core/types";
import {
  CHAT_SCROLL_BOTTOM_TOLERANCE_PX,
  CHAT_USER_MESSAGE_COLLAPSE_THRESHOLD,
} from "../core/uiConstants";
import {
  AgentConfigChip,
  AgentConfigControls,
  launchProviderReady,
  profileRunConfig,
} from "../core/AgentConfigControls";
import { SkillPicker, useSkillPicker } from "../core/SkillPicker";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { RepositoryScope } from "./RepositoryScope";
import { AgentStatusStrip, CompactionDivider } from "./AgentStatusStrip";
import { chatStatusTasks, compactionAnchors } from "./agentStatusModel";
import { BrowserTurnNotice, ChatBrowserControl } from "../core/BrowserControls";
import { WorktreeChooser, WorktreeControls, useConversationWorktree } from "./WorktreeControls";

interface Props {
  project: ProjectSnapshot;
  /** The signed-in member whose persisted drafts this chat restores and saves. */
  actorId: string | null;
  graphTarget: GraphTargetRef;
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
  runScope: string[];
  tasks: AgentTask[];
  watchers?: WatcherRecord[];
  historyMessages?: ChatMessage[];
  origin?: ChatOrigin | null;
  /** The chat summary's newest turn, which the loaded history may not reach yet. */
  agentSetup?: ChatAgentSetup | null;
  chatId: string;
  presentation?: "floating" | "workspace";
  fixedConversation?: boolean;
  readOnly?: boolean;
  readOnlyNotice?: ReactNode;
  /** Questions belong to this episode rather than the chat, as an orchestrator's do. */
  questionEpisodeId?: string;
  allowArtifactComments?: boolean;
  reviewPending?: boolean;
  graphChangesDisabled?: boolean;
  onStartTask: StartConversationTask;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onRepairGraphUpdate: (taskId: string, action?: GraphUpdateRecovery) => Promise<void>;
  onOpenNode?: (nodeId: string) => void;
  onStopWatcher?: (watcherId: string) => void;
  onNewSession: () => void;
  onClose: () => void;
  onResumeTask: (task: AgentTask) => void;
  onRetryTask: (task: AgentTask) => void;
  onRefreshTask: (taskId: string) => Promise<AgentTask>;
}

/** Whether the transcript already carries the in-flight turn as a store overlay. */
export function pendingTurnShownByHistory(
  clientId: string,
  messages: readonly { operation_id?: string | null }[],
): boolean {
  return messages.some((message) => message.operation_id === clientId);
}

interface PendingChatTurn {
  clientId: string;
  /** The start's Idempotency-Key, kept across a Resend. */
  requestId: string;
  text: string;
  /** The composer text it was sent from, which a failed send puts back. */
  draft: string;
  timestamp: string;
  mode: ConversationMode;
  attachments: ChatAttachmentDescriptor[];
  submission: ConversationTurnSubmission;
  submittedOverride: { chatId: string; config: AgentRunConfig } | null;
  /** Set when the start got no admission; the turn stays here to be resent. */
  error?: string;
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

interface SelectedChatAnnotationComposer {
  step: "comment";
  selectedText: string;
  anchor: ChatAnnotationAnchor;
  position: ChatAnnotationComposerPosition | null;
  /** Set when the selection is a part of an artifact shown inside a reply. */
  artifact?: StagedArtifactTarget;
}

interface KeyboardChatAnnotationComposer {
  step: "select";
  answerText: string;
  selectedText: string;
  anchor: ChatAnnotationAnchor;
  position: ChatAnnotationComposerPosition | null;
}

type ChatAnnotationComposer = SelectedChatAnnotationComposer | KeyboardChatAnnotationComposer;

// Where the reader left each chat in this session, kept only while they had
// scrolled up; a chat without an entry opens at its bottom.
const chatScrollPositions = new Map<string, number>();

const EMPTY_WATCHERS: WatcherRecord[] = [];
const EMPTY_HISTORY: ChatMessage[] = [];
const NO_HEADER_ACTIONS: readonly ChatHeaderAction[] = [];

function chatScrollObservation(element: HTMLElement, previousTop: number) {
  return {
    previousTop,
    top: element.scrollTop,
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
  };
}
// macOS dictation stops at 55 s, counting down the last 10; network dictation
// has no limit (see networkDictation.ts).
const DICTATION_SEGMENT_MS = 55_000;
const DICTATION_COUNTDOWN_MS = 10_000;
const SYSTEM_DICTATION_ELSEWHERE =
  "Your dictation service is macOS, which works only in the desktop app. Choose a connection in Space settings, under Dictation and voice.";

const INLINE_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024;
// One empty map, so a chat without graph nodes keeps its answers' parse memoized.
const NO_NODES: Readonly<Record<string, GraphNode>> = {};

export function reconcileChatRunScope(
  current: string[],
  requested: string[],
  projectTruthScope: string[],
  reset: boolean,
): string[] {
  const allowed = new Set(projectTruthScope);
  const candidate = reset ? requested : current;
  return candidate.filter(
    (repository, index) => allowed.has(repository) && candidate.indexOf(repository) === index,
  );
}

export function NodeChat({
  project,
  actorId,
  graphTarget,
  node,
  nodes = NO_NODES,
  glossaryIndex,
  conversationTitle,
  header,
  headerActions = NO_HEADER_ACTIONS,
  headerMenuItems,
  headerState,
  runScope,
  tasks,
  watchers = EMPTY_WATCHERS,
  historyMessages = EMPTY_HISTORY,
  origin,
  agentSetup,
  chatId,
  presentation = "floating",
  fixedConversation = false,
  readOnly = false,
  allowArtifactComments = false,
  readOnlyNotice,
  questionEpisodeId,
  reviewPending = false,
  graphChangesDisabled = false,
  onStartTask,
  onInspectTask,
  onOpenInbox,
  onRepairGraphUpdate,
  onOpenNode,
  onStopWatcher,
  onNewSession,
  onClose,
  onResumeTask,
  onRetryTask,
  onRefreshTask,
}: Props) {
  const surface = node ? "node_chat" : "project_chat";
  const skillCatalog = project.skill_catalog ?? [];
  const skillDefaults = project.skill_defaults ?? { workflow_ids: [], skill_ids: [] };
  const relatedTasks = useMemo(
    () => relatedChatTasks(tasks, surface, node?.id, chatId),
    [chatId, node?.id, surface, tasks],
  );
  const questionApiBase = `/api/projects/${encodeURIComponent(project.id)}`;
  const questionState = useQuestions(
    questionApiBase,
    questionEpisodeId ? "episode" : "chat",
    questionEpisodeId ?? chatId,
  );
  const [steeringMessages, setSteeringMessages] = useState<{
    chatId: string;
    messages: ChatMessage[];
  }>({ chatId, messages: [] });
  useEffect(() => {
    setSteeringMessages({ chatId, messages: [] });
  }, [chatId]);
  const displayedMessages = useMemo(() => {
    const messages = new Map(historyMessages.map((message) => [message.message_id, message]));
    if (steeringMessages.chatId === chatId) {
      steeringMessages.messages.forEach((message) => messages.set(message.message_id, message));
    }
    return [...messages.values()];
  }, [chatId, historyMessages, steeringMessages]);
  const [pendingTurn, setPendingTurn] = useState<PendingChatTurn | null>(null);
  const transcript = useMemo(
    () => [
      ...orderTranscriptLines([
        ...reconcileChatHistoryArtifacts(displayedMessages, relatedTasks),
        ...reconstructTaskTranscript(chatTasksMissingFromHistory(relatedTasks, displayedMessages)),
      ]),
      // The shared store already shows the sent message while its start is in
      // flight, in the transcript or as a task under the same id; this local line
      // covers only a chat the store does not show it in.
      // Not yet admitted, it stays last: a turn another tab started first may
      // carry a later server time than this browser's send time.
      ...(pendingTurn &&
      !pendingTurnShownByHistory(pendingTurn.clientId, displayedMessages) &&
      !relatedTasks.some((task) => task.operation_id === pendingTurn.clientId)
        ? [
            {
              lineId: `pending:${pendingTurn.clientId}`,
              role: "human" as const,
              text: pendingTurn.text,
              taskId: pendingTurn.clientId,
              timestamp: pendingTurn.timestamp,
              mode: pendingTurn.mode,
              attachments: pendingTurn.attachments,
              trigger: "human" as const,
            },
          ]
        : []),
    ],
    [displayedMessages, pendingTurn, relatedTasks],
  );
  const derivedConfig = useMemo(
    () =>
      latestPersistedChatConfig(
        historyMessages,
        relatedTasks,
        originRunConfig(origin, profileRunConfig(project.agent_profiles[surface])),
        agentSetup,
      ),
    [agentSetup, historyMessages, origin, project.agent_profiles, relatedTasks, surface],
  );
  // The human's pick for this chat's next turns. It is keyed by chat, so another
  // chat starts from its own default; the machine always stays the derived one.
  const [configOverride, setConfigOverride] = useState<{
    chatId: string;
    config: AgentRunConfig;
  } | null>(null);
  const [configOpen, setConfigOpen] = useState(false);
  const configChipRef = useRef<HTMLButtonElement | null>(null);
  const configPanelRef = useRef<HTMLDivElement | null>(null);
  useLayerDismiss(
    configOpen,
    () => setConfigOpen(false),
    [configChipRef, configPanelRef],
    configChipRef,
  );
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
  const [scope, setScope] = useState(() =>
    reconcileChatRunScope([], runScope, project.project_truth_scope, true),
  );
  const worktree = useConversationWorktree(
    project.id,
    chatId,
    node ? "node" : "project",
    node?.id ?? null,
    config.run_on,
    scope,
    relatedTasks.map((task) => `${task.operation_id}:${task.updated_at}`).join("\0"),
    !readOnly,
    // The conversation's own graph: an orchestrator card opened from main lives on its branch.
    graphTarget,
  );
  const scopeIdentityRef = useRef(`${project.id}\0${chatId}`);
  const requestedScopeKey = runScope.join("\0");
  const projectTruthScopeKey = project.project_truth_scope.join("\0");
  const draftKey = chatDraftStorageKey(actorId, project.id, chatId);
  const modeKey = chatModeStorageKey(project.id, chatId);
  const annotationsKey = chatAnnotationsStorageKey(actorId, project.id, chatId);
  const annotationPanelId = useId();
  const derivedMode = useMemo(
    () => latestPersistedConversationMode(historyMessages, relatedTasks),
    [historyMessages, relatedTasks],
  );
  const [draft] = useState(() => createComposerDraft(readStorage(draftKey) ?? ""));
  const [annotations, setAnnotations] = useState<StagedChatAnnotation[]>(() =>
    readStagedChatAnnotations(annotationsKey),
  );
  const artifactContext = useMemo(() => stagedArtifactContext(annotations), [annotations]);
  const canCompose = !readOnly || (allowArtifactComments && Boolean(artifactContext));
  // A read-only chat composes only artifact comments, and those edit in Discuss.
  const commentOnly = readOnly && Boolean(artifactContext);
  const [annotationComposer, setAnnotationComposer] = useState<ChatAnnotationComposer | null>(null);
  const [annotationComment, setAnnotationComment] = useState("");
  const [annotationViewport, setAnnotationViewport] =
    useState<ChatAnnotationViewportMetrics | null>(null);
  const [annotationsOpen, setAnnotationsOpen] = useState(false);
  // A finished selection inside an answer offers Comment; the composer opens
  // only when the reader asks, so the platform's Copy stays usable.
  const [selectionComment, setSelectionComment] = useState<{
    range: Range;
    position: ChatAnnotationComposerPosition;
  } | null>(null);
  const [modeState, setModeState] = useState<{ value: ConversationMode; pinned: boolean }>(() => {
    const storedMode = parseConversationMode(readStorage(modeKey));
    return { value: storedMode ?? derivedMode, pinned: Boolean(storedMode) };
  });
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [browserOn, setBrowserOn] = useState(false);
  const optionCount = Number(browserOn) + Number(worktree.chosen);
  const optionsRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!optionsOpen) return;
    const closeOutside = (event: PointerEvent) => {
      if (!optionsRef.current?.contains(event.target as Node)) setOptionsOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOptionsOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [optionsOpen]);
  const modeRef = useRef(modeState.value);
  const [submitting, setSubmitting] = useState(false);
  const referencesKey = referenceDraftKey(
    actorId,
    project.id,
    project.graph_target ?? MAIN_GRAPH,
    chatId,
  );
  const [submitError, setSubmitError] = useState<string | null>(null);
  const composer = useComposerAttachments({
    project,
    scopeId: chatId,
    referencesKey,
    onError: setSubmitError,
  });
  const { attachments, references, attachmentSetId } = composer;
  const readyAttachments = composer.ready;
  // Another chat starts with its Add menu closed.
  const { setAddMenuOpen } = composer;
  useEffect(() => setAddMenuOpen(false), [chatId, setAddMenuOpen]);
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
  // Send pressed while dictating: send once the final transcript is in the draft,
  // to the target Send addressed when pressed (a steered attempt, or a new turn).
  const [sendAfterDictation, setSendAfterDictation] = useState<{
    target: string;
  } | null>(null);
  const [expiryClock, setExpiryClock] = useState(() => Date.now());
  const [expandedHumanMessageIds, setExpandedHumanMessageIds] = useState<Set<string>>(
    () => new Set(),
  );
  const chatLinesRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const annotationCommentRef = useRef<HTMLTextAreaElement | null>(null);
  const annotationSelectionRef = useRef<HTMLTextAreaElement | null>(null);
  const annotationComposerRef = useRef<HTMLFormElement | null>(null);
  const selectionCommentRef = useRef<HTMLButtonElement | null>(null);
  const annotationOriginRef = useRef<HTMLElement | null>(null);
  // Embedded turns that aged out of the recent task list, fetched once on demand.
  // Kept here because a later bounded task-list refresh drops such tasks again.
  const [agedInlineArtifacts, setAgedInlineArtifacts] = useState<
    ReadonlyMap<string, AgentArtifactDescriptor[] | "loading">
  >(() => new Map());
  // Clears the selection mark inside an inline artifact once its comment closes.
  const inlineSelectionClearRef = useRef<(() => void) | null>(null);
  const dictationSpanRef = useRef<DictationSpan | null>(null);
  const dictationTimerRef = useRef<number | null>(null);
  const microphoneRef = useRef<{ sessionId: string; claim: MicrophoneClaim } | null>(null);
  const networkDictationRef = useRef<NetworkDictation | null>(null);
  const scrollKey = `${project.id}:${chatId}`;
  // A saved position is applied as the transcript renders, until the view
  // reaches it or the reader scrolls.
  const restoreTopRef = useRef(chatScrollPositions.get(scrollKey) ?? null);
  const shouldStickToBottomRef = useRef(restoreTopRef.current === null);
  // The last scroll position this view observed; a smaller one means the reader
  // moved up, and only the reader's own return to the bottom re-sticks.
  const lastScrollTopRef = useRef(0);
  const lastChatIdRef = useRef(scrollKey);
  const [repairingTaskId, setRepairingTaskId] = useState<string | null>(null);
  const [repairErrors, setRepairErrors] = useState<Map<string, string>>(() => new Map());
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
  const [watchersOpen, setWatchersOpen] = useState(false);
  const readiness = project.provider_readiness[config.run_on]?.[config.provider];
  const skills = useSkillPicker({
    catalog: skillCatalog,
    defaults: skillDefaults,
    provider: config.provider,
    providerLabel: readiness?.label || config.provider,
    machine: config.run_on,
    inventory: project.provider_skill_inventories?.[config.run_on]?.[config.provider],
    getMessage: draft.get,
    onComplete: (next) => {
      draft.set(next);
      setSubmitError(null);
      window.requestAnimationFrame(() => {
        textareaRef.current?.focus();
        textareaRef.current?.setSelectionRange(next.length, next.length);
      });
    },
  });
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const relatedActive = relatedTasks.some(isActiveTask);
  const statusTasks = useMemo(
    () => chatStatusTasks(relatedTasks, displayedMessages),
    [relatedTasks, displayedMessages],
  );
  const compactionByLine = useMemo(
    () => compactionAnchors(transcript, statusTasks),
    [statusTasks, transcript],
  );
  // A settled turn may have published a new version of an artifact shown in place.
  const inlineArtifactRefreshToken = useMemo(
    () =>
      relatedTasks
        .filter((task) => !isActiveTask(task))
        .map((task) => `${task.operation_id}:${task.status}:${task.updated_at}`)
        .join("|"),
    [relatedTasks],
  );
  // While the watched turn can take live input, the ordinary composer addresses
  // that exact attempt instead of starting a new turn. There is no separate
  // steering control; a runtime without an input channel leaves the composer
  // unavailable exactly as any other running turn does.
  const steeringTask = [...relatedTasks]
    .reverse()
    .find((task) => task.active && task.steer_visible && task.can_steer && task.steer_turn_id);
  const awaitingSteerReceipt = Boolean(steeringTask) && submitting;
  const apiBase = `/api/projects/${encodeURIComponent(project.id)}`;
  const watcherRows = useMemo(
    () => visibleChatWatchers(watchers, chatId, node, graphTarget),
    [chatId, node, watchers, graphTarget],
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
  // Earlier receipts of one task stay as history; only its latest offers recovery.
  const latestGraphReceiptLineIds = useMemo(() => {
    const latest = new Map<string, string>();
    for (const line of transcript) {
      if (line.role === "agent" && line.graphUpdate) latest.set(line.taskId, line.lineId);
    }
    return new Set(latest.values());
  }, [transcript]);
  const pausedAttempt = resumablePausedChatTask(relatedTasks);
  const sessionId = resolvedChatSessionId(relatedTasks);
  const freshProviderSession = providerSwitchStartsFreshSession(
    relatedTasks,
    sessionId,
    config.provider,
  );
  // A follow-up joins the running turn, so the composer shows that turn's mode.
  const steeringMode = steeringTask ? parseConversationMode(steeringTask.request.mode) : null;
  const mode = commentOnly ? "discuss" : (steeringMode ?? modeState.value);
  modeRef.current = mode;
  // A Work turn also needs the provider's Work probe; Discuss does not.
  const providerReady = launchProviderReady(project, config, mode === "work");
  const chatTitle = node?.title || conversationTitle || project.name;
  const attachmentClientId = composer.clientId;
  const attachDisabled = composer.full || composer.preparing || submitting || awaitingSteerReceipt;
  const attachmentsUnready = composer.unready;
  const annotationComposerOpen = annotationComposer !== null;
  const annotationComposerOpenRef = useRef(annotationComposerOpen);
  annotationComposerOpenRef.current = annotationComposerOpen;
  const annotationsComplete = stagedChatAnnotationsAreComplete(annotations);
  const dictating = dictationState !== "idle" && dictationState !== "error";
  const kept = useKeptSpeech(draftKey);
  // The chat re-renders when the draft becomes sendable or empty, not per keystroke.
  const draftSendable = useComposerDraft(draft, (text) =>
    Boolean(assembleChatTurn(text, annotations)),
  );
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
    const identity = `${project.id}\0${chatId}`;
    const reset = scopeIdentityRef.current !== identity;
    scopeIdentityRef.current = identity;
    setScope((current) =>
      reconcileChatRunScope(current, runScope, project.project_truth_scope, reset),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the scope arrays are keyed by content so a new array with the same members keeps the user's choice
  }, [chatId, project.id, projectTruthScopeKey, requestedScopeKey]);

  useEffect(() => {
    setModeState((current) =>
      current.pinned || current.value === derivedMode
        ? current
        : { ...current, value: derivedMode },
    );
  }, [derivedMode]);

  useEffect(() => {
    setOptionsOpen(false);
  }, [chatId, project.id]);

  useEffect(() => {
    skills.reset();
    // Settings supplies fresh conversation defaults; an open turn keeps its own.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset only when the chat changes, not when the picker object does
  }, [chatId, project.id]);

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
    if (annotations.length) writeSessionStorage(annotationsKey, JSON.stringify(annotations));
    else removeSessionStorage(annotationsKey);
  }, [annotations, annotationsKey]);

  useEffect(() => {
    if (!annotationComposerOpen) {
      setAnnotationViewport(null);
      return;
    }
    const viewport = window.visualViewport;
    const update = () => {
      setAnnotationViewport(
        chatAnnotationViewportMetrics(
          { width: window.innerWidth, height: window.innerHeight },
          viewport,
        ),
      );
    };
    update();
    viewport?.addEventListener("resize", update);
    viewport?.addEventListener("scroll", update);
    window.addEventListener("resize", update);
    return () => {
      viewport?.removeEventListener("resize", update);
      viewport?.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [annotationComposerOpen]);

  useLayoutEffect(() => {
    const composer = annotationComposerRef.current;
    if (!composer || !annotationComposer || !annotationViewport) return;
    const update = () => {
      const rect = composer.getBoundingClientRect();
      const position = chatAnnotationComposerPosition(
        annotationComposer.anchor,
        annotationViewport,
        rect,
      );
      setAnnotationComposer((current) => {
        if (
          !current ||
          current.anchor !== annotationComposer.anchor ||
          (current.position?.left === position.left && current.position.top === position.top)
        )
          return current;
        return { ...current, position };
      });
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(composer);
    return () => observer.disconnect();
  }, [annotationComposer, annotationViewport]);

  // The composer stays hidden until it is placed, and a hidden field cannot take
  // focus, so the step's field is focused once the composer becomes visible.
  const annotationComposerPlaced = Boolean(annotationComposer?.position);
  const annotationComposerStep = annotationComposer?.step;
  useEffect(() => {
    if (!annotationComposerPlaced) return;
    if (annotationComposerRef.current?.contains(document.activeElement)) return;
    const field =
      annotationComposerStep === "select"
        ? annotationSelectionRef.current
        : annotationCommentRef.current;
    field?.focus();
    if (annotationComposerStep === "select") field?.setSelectionRange(0, 0);
  }, [annotationComposerPlaced, annotationComposerStep]);

  useEffect(() => {
    if (lastChatIdRef.current !== scrollKey) {
      lastChatIdRef.current = scrollKey;
      restoreTopRef.current = chatScrollPositions.get(scrollKey) ?? null;
      shouldStickToBottomRef.current = restoreTopRef.current === null;
      setRepositoryFileErrors(new Map());
    }
    const element = chatLinesRef.current;
    if (!element) return;
    if (restoreTopRef.current !== null) {
      element.scrollTop = restoreTopRef.current;
      lastScrollTopRef.current = element.scrollTop;
      if (Math.abs(element.scrollTop - restoreTopRef.current) < 1) restoreTopRef.current = null;
      return;
    }
    // A refresh can render between the reader's scroll and its scroll event; their
    // position wins over a follow decided before they moved.
    if (chatReaderMovedUp(chatScrollObservation(element, lastScrollTopRef.current)))
      shouldStickToBottomRef.current = false;
    if (!shouldStickToBottomRef.current) return;
    element.scrollTop = element.scrollHeight;
    lastScrollTopRef.current = element.scrollTop;
    // Question cards load after the transcript and end it, so they move the bottom too.
  }, [scrollKey, transcript, questionState.questions]);

  useEffect(() => {
    if (!transcript.some((line) => line.attachments?.length)) return;
    const timer = window.setInterval(() => setExpiryClock(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, [transcript]);

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

  const selectMode = useCallback(
    (next: ConversationMode) => {
      modeRef.current = next;
      writeStorage(modeKey, next);
      setModeState({ value: next, pinned: true });
    },
    [modeKey],
  );

  const toggleMode = useCallback(() => {
    // A follow-up inherits the running turn's capability, so the toggle would
    // describe something it cannot change. While a turn merely runs unsteerable,
    // the choice still belongs to the next turn and stays available.
    if (steeringTask || commentOnly) return;
    selectMode(toggleConversationMode(modeRef.current));
  }, [steeringTask, commentOnly, selectMode]);

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
    void send();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fires on dictation end only
  }, [sendAfterDictation, dictating]);

  useEffect(() => {
    if (presentation !== "workspace" || readOnly) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat) return;
      if (!isConversationModeShortcut(event.key, event.shiftKey)) return;
      event.preventDefault();
      toggleMode();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [presentation, readOnly, toggleMode]);

  const updateMessage = (next: string) => {
    if (readOnly) return;
    // Pin the next turn's pick, not a running turn's mode shown while steering.
    if (next && !modeState.pinned && !commentOnly) {
      writeStorage(modeKey, modeState.value);
      setModeState({ value: modeState.value, pinned: true });
    }
    draft.set(next);
    skills.readMessage(next);
    setSubmitError(null);
  };

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
    if (!commentOnly) {
      writeStorage(modeKey, modeState.value);
      setModeState({ value: modeState.value, pinned: true });
    }
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

  const addFiles = (incoming: File[]) => {
    if (readOnly) return;
    void composer.addFiles(incoming);
  };

  const addReference = (item: DraftReference) => {
    if (submitting || commentOnly) return;
    composer.addReference(item);
  };
  const insertReferenceText: InsertReferenceText = (
    text,
    fileCount = attachments.length,
    insertPlainText = false,
  ) => {
    if (submitting || commentOnly) return false;
    return composer.insertReferenceText(text, {
      fileCount,
      insertPlainText,
      input: textareaRef.current,
      current: draft.get(),
      update: (next) => {
        if (dictating) stopDictation(true);
        updateMessage(next);
      },
    });
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
    // A restore the transcript was still too short to reach is not the reader's move.
    if (restoreTopRef.current !== null) {
      if (element.scrollTop === lastScrollTopRef.current) return;
      restoreTopRef.current = null;
    }
    shouldStickToBottomRef.current = chatFollowsBottom(
      chatScrollObservation(element, lastScrollTopRef.current),
      CHAT_SCROLL_BOTTOM_TOLERANCE_PX,
    );
    lastScrollTopRef.current = element.scrollTop;
    if (shouldStickToBottomRef.current) chatScrollPositions.delete(scrollKey);
    else chatScrollPositions.set(scrollKey, element.scrollTop);
  };

  const openAnnotationComposer = (range: Range) => {
    if (submitting) return;
    const selectedText = range.toString().trim();
    if (!selectedText) return;
    if (selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH) {
      setSubmitError(
        `Select at most ${MAX_CHAT_ANNOTATION_TEXT_LENGTH.toLocaleString()} characters for one comment.`,
      );
      return;
    }
    if (annotations.length >= MAX_CHAT_ANNOTATIONS) {
      setSubmitError(`A turn can include at most ${MAX_CHAT_ANNOTATIONS} annotations.`);
      return;
    }
    // Show the reader the text that will be staged, not the overshoot.
    const selection = window.getSelection();
    if (selection) {
      selection.removeAllRanges();
      selection.addRange(range);
    }
    const rects = range.getClientRects();
    const rect = rects.item(rects.length - 1) ?? range.getBoundingClientRect();
    annotationOriginRef.current = null;
    setAnnotationComment("");
    setSubmitError(null);
    setAnnotationComposer({
      step: "comment",
      selectedText,
      anchor: { left: rect.left, right: rect.right, top: rect.top },
      position: null,
    });
    window.requestAnimationFrame(() => annotationCommentRef.current?.focus());
  };

  // The pointer often lifts outside the answer that was swept, so the release is
  // observed on the document and the answer is resolved from the selection itself.
  // Touch selection (long press, dragged handles) settles without a pointerup, so
  // a selection change made while no pointer is down refreshes the offer too.
  useEffect(() => {
    if (readOnly) {
      setSelectionComment(null);
      return;
    }
    let pointerDown = false;
    const refresh = () => {
      // The composer re-selects the staged text; that is not a new offer.
      if (annotationComposerOpenRef.current) return setSelectionComment(null);
      const range = annotatableAnswerSelectionRange(window.getSelection(), chatLinesRef.current);
      const rects = range?.getClientRects();
      const firstLine = rects?.item(0) ?? range?.getBoundingClientRect();
      if (!range || !firstLine) {
        setSelectionComment(null);
        return;
      }
      const button = selectionCommentRef.current?.getBoundingClientRect();
      const viewport = chatAnnotationViewportMetrics(
        { width: window.innerWidth, height: window.innerHeight },
        window.visualViewport,
      );
      setSelectionComment({
        range,
        position: chatSelectionCommentPosition(
          { firstLine, bottom: range.getBoundingClientRect().bottom },
          viewport,
          button?.width ? button : { width: 96, height: 32 },
        ),
      });
    };
    const insideOwnControls = (target: EventTarget | null) =>
      target instanceof Node &&
      Boolean(
        annotationComposerRef.current?.contains(target) ||
        selectionCommentRef.current?.contains(target),
      );
    const onPointerDown = (event: PointerEvent) => {
      if (!insideOwnControls(event.target)) pointerDown = true;
    };
    const onPointerUp = (event: PointerEvent) => {
      pointerDown = false;
      // Clicks inside the open composer or on Comment must not restart the offer.
      if (insideOwnControls(event.target)) return;
      refresh();
    };
    const onPointerCancel = () => {
      pointerDown = false;
    };
    const onSelectionChange = () => {
      if (!pointerDown) refresh();
    };
    const onViewportChange = () => {
      if (!pointerDown) refresh();
    };
    // Capture phase: a release over a window resize corner stops propagation.
    document.addEventListener("pointerdown", onPointerDown, { capture: true });
    document.addEventListener("pointerup", onPointerUp, { capture: true });
    document.addEventListener("pointercancel", onPointerCancel, { capture: true });
    document.addEventListener("selectionchange", onSelectionChange);
    document.addEventListener("scroll", onViewportChange, { capture: true, passive: true });
    // A resize, rotation, or soft keyboard moves the selection under a fixed offer.
    const viewport = window.visualViewport;
    window.addEventListener("resize", onViewportChange);
    viewport?.addEventListener("resize", onViewportChange);
    viewport?.addEventListener("scroll", onViewportChange);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, { capture: true });
      document.removeEventListener("pointerup", onPointerUp, { capture: true });
      document.removeEventListener("pointercancel", onPointerCancel, { capture: true });
      document.removeEventListener("selectionchange", onSelectionChange);
      document.removeEventListener("scroll", onViewportChange, { capture: true });
      window.removeEventListener("resize", onViewportChange);
      viewport?.removeEventListener("resize", onViewportChange);
      viewport?.removeEventListener("scroll", onViewportChange);
    };
  }, [readOnly]);

  const openKeyboardAnnotationComposer = (answer: HTMLElement, origin: HTMLElement) => {
    if (submitting) return;
    if (annotations.length >= MAX_CHAT_ANNOTATIONS) {
      setSubmitError(`A turn can include at most ${MAX_CHAT_ANNOTATIONS} annotations.`);
      return;
    }
    const answerText = answer.innerText.trim();
    if (!answerText) return;
    const rect = origin.getBoundingClientRect();
    annotationOriginRef.current = origin;
    setAnnotationComment("");
    setSubmitError(null);
    setAnnotationComposer({
      step: "select",
      answerText,
      selectedText: "",
      anchor: { left: rect.left, right: rect.right, top: rect.top },
      position: null,
    });
    window.requestAnimationFrame(() => {
      annotationSelectionRef.current?.focus();
      annotationSelectionRef.current?.setSelectionRange(0, 0);
    });
  };

  const updateKeyboardAnnotationSelection = (control: HTMLTextAreaElement) => {
    const selectedText = chatAnnotationTextControlSelection(control);
    setAnnotationComposer((current) =>
      current?.step === "select" ? { ...current, selectedText } : current,
    );
  };

  const continueKeyboardAnnotation = () => {
    if (submitting) return;
    if (annotationComposer?.step !== "select") return;
    const selectedText = annotationComposer.selectedText;
    if (!selectedText || selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH) return;
    setAnnotationComposer({
      step: "comment",
      selectedText,
      anchor: annotationComposer.anchor,
      position: null,
    });
    window.requestAnimationFrame(() => annotationCommentRef.current?.focus());
  };

  // A part of an artifact shown inside a reply, selected in its comment mode, is
  // staged like answer text; the turn carries it as that artifact's selection.
  const openInlineArtifactComment = (
    taskId: string,
    artifact: AgentArtifactDescriptor,
    event: InlineArtifactSelectionEvent,
  ) => {
    if (submitting) {
      event.clear();
      return;
    }
    if (annotations.length >= MAX_CHAT_ANNOTATIONS) {
      setSubmitError(`A turn can include at most ${MAX_CHAT_ANNOTATIONS} annotations.`);
      event.clear();
      return;
    }
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
    const draft = annotationComposer?.step === "comment" ? annotationComment.trim() : "";
    const prefill = event.initialText ?? "";
    if (
      draft &&
      prefill &&
      annotationComposer?.step === "comment" &&
      annotationComposer.artifact?.context.artifact_id === artifact.artifact_id
    ) {
      if (!draft.includes(prefill)) setAnnotationComment(`${draft}\n\n${prefill}`);
      window.requestAnimationFrame(() => annotationCommentRef.current?.focus());
      return;
    }
    inlineSelectionClearRef.current?.();
    inlineSelectionClearRef.current = event.clear;
    annotationOriginRef.current = null;
    setAnnotationComment(prefill);
    setSubmitError(null);
    setAnnotationComposer({
      step: "comment",
      selectedText: event.description || artifact.name,
      anchor: event.anchor,
      position: null,
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
    });
    window.requestAnimationFrame(() => annotationCommentRef.current?.focus());
  };

  // A selection cancelled inside the frame closes the comment staged from it.
  const cancelInlineArtifactComment = (taskId: string, artifact: AgentArtifactDescriptor) => {
    const context =
      annotationComposer?.step === "comment" ? annotationComposer.artifact?.context : undefined;
    if (context?.operation_id !== taskId || context.artifact_id !== artifact.artifact_id) return;
    // The frame already cleared its mark; a clear sent now would end the drag that
    // a new selection starts with.
    inlineSelectionClearRef.current = null;
    dismissAnnotationComposer(false);
  };

  // A new version moves the parts a selection named, so comments on the old one,
  // open or staged, would be sent against bytes the reader never saw.
  const dropStaleArtifactComments = (artifact: AgentArtifactDescriptor) => {
    const open =
      annotationComposer?.step === "comment" ? annotationComposer.artifact?.context : undefined;
    if (open?.artifact_id === artifact.artifact_id) {
      inlineSelectionClearRef.current = null;
      dismissAnnotationComposer(false);
    }
    const stale = annotations.filter(
      (annotation) => annotation.artifact?.context.artifact_id === artifact.artifact_id,
    );
    if (!stale.length) return;
    setAnnotations((current) => current.filter((annotation) => !stale.includes(annotation)));
    setSubmitError(
      `${artifact.name} changed to a new version, so its staged comments were removed. Select the parts again.`,
    );
  };

  const dismissAnnotationComposer = (returnFocus: boolean) => {
    const origin = annotationOriginRef.current;
    annotationOriginRef.current = null;
    inlineSelectionClearRef.current?.();
    inlineSelectionClearRef.current = null;
    setAnnotationComposer(null);
    setAnnotationComment("");
    if (returnFocus) {
      window.requestAnimationFrame(() => (origin ?? textareaRef.current)?.focus());
    }
  };

  const stageAnnotation = () => {
    if (submitting) return;
    if (!annotationComposer) return;
    if (annotationComposer.step !== "comment") return;
    const comment = annotationComment.trim();
    if (!comment) return;
    const artifact = annotationComposer.artifact;
    setAnnotations((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        selectedText: annotationComposer.selectedText,
        comment,
        ...(artifact ? { artifact } : {}),
      },
    ]);
    setAnnotationsOpen(false);
    dismissAnnotationComposer(false);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const updateAnnotation = (id: string, comment: string) => {
    if (submitting) return;
    setAnnotations((current) =>
      current.map((annotation) => (annotation.id === id ? { ...annotation, comment } : annotation)),
    );
    setSubmitError(null);
  };

  const removeAnnotation = (id: string) => {
    if (submitting) return;
    setAnnotations((current) => {
      const next = current.filter((annotation) => annotation.id !== id);
      if (next.length === 0) setAnnotationsOpen(false);
      return next;
    });
  };

  const steer = async (task: AgentTask) => {
    const draftMessage = draft.get();
    const draftAnnotations = annotations;
    const text = assembleChatTurn(draftMessage, annotations);
    if (!annotationsComplete) {
      setAnnotationsOpen(true);
      setSubmitError("Each staged annotation needs a comment.");
      return;
    }
    // Files and artifact selections are staged for a new turn; a steer carries text only.
    if (
      !text ||
      attachments.length ||
      references.length ||
      artifactContext ||
      submitting ||
      !task.steer_turn_id
    )
      return;
    if (dictating) stopDictation(true);
    shouldStickToBottomRef.current = true;
    restoreTopRef.current = null;
    const request = {
      message_id: crypto.randomUUID(),
      attempt: task.attempt,
      expected_turn_id: task.steer_turn_id,
      message: text,
    };
    setSubmitError(null);
    setSubmitting(true);
    try {
      const receipt = await steerChatTurn(project.id, task.operation_id, request);
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
      // the delivered draft is exactly what is consumed. Artifact comments can
      // still arrive from another view meanwhile, and they survive.
      draft.set((current) => (current === draftMessage ? "" : current));
      setAnnotations((current) => (current === draftAnnotations ? [] : current));
      setAnnotationsOpen(false);
      skills.reset();
    } catch (error) {
      setSubmitError(
        `Steering receipt could not be read. Nothing was resent. ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      setSubmitting(false);
    }
  };

  /** Everything a new turn launches with besides its text, for a dictated Send. */
  const launchSettings = () =>
    JSON.stringify([
      mode,
      config,
      scope,
      artifactContext,
      references.map((item) => item.selector),
      attachments.map((item) => `${item.localId}:${item.status}`),
      attachmentSetId,
      skills.selection,
      mode === "work" && worktree.chosen,
    ]);

  const send = async () => {
    if (!canCompose) return;
    if (dictating) {
      setSendAfterDictation({ target: sendTarget(steeringTask, launchSettings(), relatedActive) });
      if (dictationState !== "stopping" && dictationState !== "transcribing") stopDictation();
      return;
    }
    if (steeringTask) return steer(steeringTask);
    if (mode === "work" && worktree.chosen && !worktree.state?.can_choose) {
      setSubmitError(
        worktree.error ??
          worktree.state?.unavailable_reason ??
          "Worktree eligibility has not been confirmed.",
      );
      return;
    }
    const draftMessage = draft.get();
    const text = assembleChatTurn(draftMessage, annotations);
    if (!annotationsComplete) {
      setAnnotationsOpen(true);
      setSubmitError("Each staged annotation needs a comment.");
      return;
    }
    if (
      !(text || artifactContext) ||
      attachmentsUnready ||
      relatedActive ||
      pausedAttempt ||
      submitting ||
      repairingTaskId ||
      reviewPending ||
      // Enter reaches here without the button, so a new turn checks readiness itself.
      !providerReady
    )
      return;
    if (!confirmDiscardKeptSpeech("send")) return;
    if (dictating) stopDictation(true);
    // A new send is a new request; only Resend reuses a failed turn's key.
    const requestId = crypto.randomUUID();
    draft.set("");
    setAddMenuOpen(false);
    await submitTurn({
      clientId: `pending-${requestId}`,
      requestId,
      text,
      draft: draftMessage,
      timestamp: new Date().toISOString(),
      mode,
      attachments: readyAttachments,
      submission: {
        kind: surface,
        config,
        runTruthScope: scope,
        nodeId: node?.id ?? null,
        message: text,
        chatId,
        sessionId,
        mode,
        artifactContext,
        references: references.map((item) => item.selector),
        attachmentSetId: readyAttachments.length ? attachmentSetId : null,
        attachmentClientId: readyAttachments.length ? attachmentClientId : null,
        skills: skills.selection,
        providerSkillNames: skills.providerSkillNames,
        worktree: mode === "work" && worktree.chosen,
      },
      submittedOverride: configOverride,
    });
  };

  /** Start a turn, or start it again after a failed send under the same key. */
  const submitTurn = async (turn: PendingChatTurn) => {
    const pending = { ...turn, error: undefined };
    shouldStickToBottomRef.current = true;
    restoreTopRef.current = null;
    setPendingTurn(pending);
    setSubmitError(null);
    setSubmitting(true);
    try {
      await startConversationTurn(onStartTask, turn.submission, turn.requestId);
      // The admitted turn now carries the pick, so the chat follows its history
      // again; a newer pick made while submitting stays.
      setConfigOverride((current) => (current === turn.submittedOverride ? null : current));
      worktree.refresh();
      setPendingTurn((current) => (current?.clientId === turn.clientId ? null : current));
      draft.set((current) => (current === turn.draft ? "" : current));
      skills.reset();
      composer.clear();
      setAnnotations([]);
      setAnnotationsOpen(false);
      removeSessionStorage(annotationsKey);
      if (!commentOnly) selectMode(turn.mode);
    } catch (error) {
      // A replaced backend admitted nothing and Reconnect is already offered, so
      // the text returns to the composer with no failed turn to resend. The
      // changed backend may already have unmounted this chat, so the stored draft
      // carries the text to the composer Reconnect reopens.
      if (error instanceof ApiError && error.code === "backend_instance_replaced") {
        if (!readStorage(draftKey)) writeStorage(draftKey, turn.draft);
        setPendingTurn((current) => (current?.clientId === turn.clientId ? null : current));
        draft.set((current) => (current ? current : turn.draft));
        return;
      }
      // The request may never have reached the server, so the turn stays where it
      // was sent, with Resend under the same key: at most one turn is admitted.
      // The composer gets the text back too, so it survives leaving this chat.
      // fetch rejects a lost connection with a TypeError and the start timeout
      // with a TimeoutError; neither message tells the reader anything.
      const lost =
        error instanceof TypeError ||
        (error instanceof DOMException && error.name === "TimeoutError");
      const reason = lost
        ? "Not sent: the connection to RCP dropped."
        : error instanceof Error
          ? error.message
          : String(error);
      setPendingTurn((current) =>
        current?.clientId === turn.clientId ? { ...pending, error: reason } : current,
      );
      draft.set((current) => (current ? current : turn.draft));
    } finally {
      setSubmitting(false);
    }
  };

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
      await startConversationTurn(onStartTask, {
        kind: surface,
        config,
        runTruthScope: scope,
        nodeId: node?.id ?? null,
        message: option.label,
        chatId,
        sessionId,
        mode: "work",
        worktreeIntegration: option.id,
      });
      selectMode("work");
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
      await onRepairGraphUpdate(taskId, action);
    } catch (error) {
      setRepairErrors((current) =>
        withMapValue(current, taskId, error instanceof Error ? error.message : String(error)),
      );
    } finally {
      setRepairingTaskId(null);
    }
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
    if (artifact.view !== "pdf") {
      openArtifactPanel({ projectId: project.id, artifactId: artifact.artifact_id });
      return;
    }
    const key = `${taskId}:${artifact.artifact_id}`;
    setArtifactShellErrors((current) => withoutMapKey(current, key));
    try {
      if (desktop) {
        await openDesktopArtifactPdf({
          projectId: project.id,
          taskId,
          artifactId: artifact.artifact_id,
        });
      }
    } catch (error) {
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

  const keepArtifact = async (taskId: string, artifact: AgentArtifactDescriptor) => {
    const key = `${taskId}:${artifact.artifact_id}`;
    if (!artifact.can_keep || keepingArtifacts.has(key)) return;
    setKeepingArtifacts((current) => new Set(current).add(key));
    setArtifactShellErrors((current) => withoutMapKey(current, key));
    try {
      await api(artifactUrl(project.id, taskId, artifact.artifact_id, "keep"), {
        method: "POST",
        body: JSON.stringify({}),
      });
      const task = await onRefreshTask(taskId);
      // An aged-out turn's embed reads its cached descriptors, which still offer Keep.
      setAgedInlineArtifacts((current) =>
        Array.isArray(current.get(taskId))
          ? new Map(current).set(taskId, taskArtifacts(task))
          : current,
      );
    } catch (error) {
      setArtifactShellErrors((current) =>
        withMapValue(current, key, error instanceof Error ? error.message : String(error)),
      );
    } finally {
      setKeepingArtifacts((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
    }
  };

  const openRepositoryFile = async (messageId: string, taskId: string, href: string) => {
    const resolution = resolveRepositoryFileHref(href, project.repositories);
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
        const artifact = found?.artifact;
        if (found && artifact) {
          const owner = found.taskId;
          setRepositoryFileErrors((current) => withoutMapKey(current, messageId));
          if (artifact.can_open || (desktop && artifact.view === "pdf" && artifact.can_download)) {
            await openArtifact(owner, artifact);
          } else {
            if (!relatedTasks.some((candidate) => candidate.operation_id === owner))
              await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
            const card = document.getElementById(`artifact-${owner}-${artifact.artifact_id}`);
            card?.scrollIntoView({ block: "nearest" });
            card?.focus();
          }
          return;
        }
      }
      // A path no repository or turn folder owns, such as an API route, is an inert link.
      if (resolution.reason !== "no-match")
        setRepositoryFileErrors((current) => withMapValue(current, messageId, resolution.message));
      return;
    }

    setRepositoryFileErrors((current) => withoutMapKey(current, messageId));
    openRepositoryFilePanel({
      projectId: project.id,
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
        href={artifactUrl(project.id, taskId, artifact.artifact_id, "download")}
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
        setAgedInlineArtifacts((current) => new Map(current).set(taskId, loaded));
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
    if (!found) return null;
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
        projectId={project.id}
        artifact={artifact}
        title={title}
        refreshToken={inlineArtifactRefreshToken}
        canComment={!readOnly || allowArtifactComments}
        keeping={keepingArtifacts.has(key)}
        actionError={artifactShellErrors.get(key)}
        onLoadTask={onLoadTask}
        onExpand={() => artifact && void openArtifact(taskId, artifact)}
        onKeep={artifact?.can_keep ? () => void keepArtifact(taskId, artifact) : null}
        download={artifact && artifactDownloadControl(taskId, artifact)}
        onSelection={(event) => artifact && openInlineArtifactComment(taskId, artifact, event)}
        onSelectionCancel={() => artifact && cancelInlineArtifactComment(taskId, artifact)}
        onVersionChange={() => artifact && dropStaleArtifactComments(artifact)}
      />
    );
  };

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

  const profile = project.agent_profiles[surface];
  const effectiveModel = config.provider === profile.provider ? profile.effective_model : "";
  const agentSummary = (
    <AgentConfigChip
      ref={configChipRef}
      project={project}
      value={config}
      effectiveModel={effectiveModel}
      open={configOpen}
      disabled={readOnly || pendingFork}
      label="Chat agent"
      workLike={mode === "work"}
      onToggle={() => setConfigOpen((open) => !open)}
    />
  );

  const contextControls = (showProvider: boolean) => (
    <div className="chat-context-controls">
      {(showProvider || !readOnly) && agentSummary}
      <AgentStatusStrip tasks={statusTasks} />
      {presentation === "workspace" && watcherToggle}
      <ChatHeaderMenu
        actions={[
          ...headerActions,
          ...(!fixedConversation && !readOnly
            ? [{ label: "New session", onSelect: onNewSession }]
            : []),
        ]}
      >
        {headerMenuItems}
        {!readOnly && (
          <div className="chat-scope-control">
            <RepositoryScope
              repositories={project.repositories}
              projectScope={project.project_truth_scope}
              stateRepository={project.state_repository}
              selected={scope}
              onChange={relatedActive || reviewPending ? () => undefined : setScope}
            />
          </div>
        )}
      </ChatHeaderMenu>
    </div>
  );

  return (
    <div
      className={`chat-dock ${presentation}`}
      data-mode={mode}
      role={presentation === "floating" ? "dialog" : "region"}
      aria-modal="false"
      aria-label={node || conversationTitle ? `Chat about ${chatTitle}` : "Project chat"}
      aria-keyshortcuts={presentation === "workspace" && !readOnly ? "Shift+Tab" : undefined}
    >
      {presentation === "floating" && (
        <header data-drag-handle="true">
          <MessageCircle size={16} />
          <strong>{chatTitle}</strong>
          {watcherToggle}
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Minimize chat; background work will continue"
          >
            <X size={16} />
          </button>
        </header>
      )}
      {header ? (
        <header className="conversation-header" data-state={headerState}>
          <div className="conversation-heading">{header}</div>
          {contextControls(false)}
        </header>
      ) : (
        contextControls(true)
      )}
      {configOpen && !readOnly && (
        <AgentConfigControls
          ref={configPanelRef}
          project={project}
          value={config}
          onChange={(next) => setConfigOverride({ chatId, config: next })}
          effectiveModel={effectiveModel}
          workLikeCapable={profile.work_like_capable}
          showRunOn={false}
          compact
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
                  <ExternalJobRow apiBase={apiBase} watcher={watcher} />
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
      <div
        className="node-chat-lines"
        aria-live="polite"
        onScroll={handleChatScroll}
        ref={chatLinesRef}
      >
        {origin && <ChatOriginLink projectId={project.id} origin={origin} />}
        {questionTranscript(transcript, questionState.questions).map((entry) => {
          if (entry.kind === "question")
            return (
              <QuestionCard
                key={entry.question.question_id}
                question={entry.question}
                apiBase={questionApiBase}
                onResolved={questionState.refresh}
              />
            );
          const line = entry.line;
          const messageId = line.lineId;
          const task = relatedTasks.find((candidate) => candidate.operation_id === line.taskId);
          const activeLineTask = task && !line.steering && isActiveTask(task) ? task : null;
          const pausedLineTask =
            !line.steering &&
            task?.paused &&
            task.can_resume &&
            !continuedTaskIds.has(task.operation_id)
              ? task
              : null;
          // Only the newest turn's error row carries Retry; the header shows no second one.
          const retryLineTask =
            line.role === "error" &&
            task?.can_retry &&
            !task.can_resume &&
            isNewestOfTurnClass(task, relatedTasks)
              ? task
              : null;
          const pendingLine = pendingTurn?.clientId === line.taskId;
          const failedTurn = pendingLine && pendingTurn?.error ? pendingTurn : null;
          const collapsible =
            line.role === "human" && line.text.length > CHAT_USER_MESSAGE_COLLAPSE_THRESHOLD;
          const expanded = expandedHumanMessageIds.has(messageId);
          const artifactComments = line.artifactComments;
          const commentLead = artifactComments
            ? artifactCommentLead(line.text, artifactComments)
            : null;
          const compaction = compactionByLine.get(line.lineId);
          return (
            <Fragment key={line.lineId}>
              {compaction?.placement === "before" && (
                <CompactionDivider compactions={compaction.compactions} />
              )}
              <div className={`node-chat-line ${line.role}${failedTurn ? " unsent" : ""}`}>
                <BrowserTurnNotice status={line.browserStatus} turnId={line.taskId} />
                {line.role === "human" && line.mode && (
                  <span className={`chat-turn-mode ${line.mode}`}>{modeLabel(line.mode)}</span>
                )}
                {failedTurn && (
                  <button
                    type="button"
                    className="icon-button chat-turn-resend"
                    aria-label="Resend"
                    title={failedTurn.error}
                    disabled={submitting}
                    onClick={() => void submitTurn(failedTurn)}
                  >
                    <RotateCcw size={14} />
                  </button>
                )}
                {line.trigger === "watcher" && (
                  <span className="chat-turn-trigger watcher">Watcher</span>
                )}
                {line.role === "agent" ? (
                  line.running ? (
                    activeLineTask && <InlineTaskProgress task={activeLineTask} />
                  ) : (
                    line.text && (
                      <>
                        <div className="chat-markdown chat-annotatable-answer">
                          <MarkdownAnswer
                            text={line.text}
                            nodes={nodes}
                            glossaryIndex={glossaryIndex}
                            onOpenNode={onOpenNode}
                            onOpenRepositoryFileLink={(href) =>
                              void openRepositoryFile(messageId, line.taskId, href)
                            }
                            renderEmbed={(src, alt) =>
                              renderInlineArtifact(line.taskId, line.artifacts, src, alt)
                            }
                          />
                        </div>
                        {/* Outside the annotatable wrapper: a selection clamp or the
                        keyboard flow must never stage this diagnostic as answer text. */}
                        {repositoryFileErrors.get(messageId) && (
                          <strong className="chat-repository-file-error" role="alert">
                            {repositoryFileErrors.get(messageId)}
                          </strong>
                        )}
                        {!readOnly && (
                          <button
                            className="chat-answer-annotation-button"
                            type="button"
                            aria-label="Comment on this answer"
                            disabled={submitting}
                            onClick={(event) => {
                              const answer =
                                event.currentTarget.parentElement?.querySelector<HTMLElement>(
                                  ".chat-annotatable-answer",
                                );
                              if (answer)
                                openKeyboardAnnotationComposer(answer, event.currentTarget);
                            }}
                          >
                            <MessageCirclePlus size={12} /> Comment
                          </button>
                        )}
                      </>
                    )
                  )
                ) : line.role === "human" ? (
                  <>
                    <div
                      className={`chat-human-message${collapsible && !expanded ? " collapsed" : ""}`}
                    >
                      {artifactComments && commentLead !== null ? (
                        <ArtifactCommentQuotes
                          lead={commentLead}
                          comments={artifactComments}
                          onOpen={() =>
                            openArtifactPanel({
                              projectId: project.id,
                              artifactId: artifactComments.artifact_id,
                            })
                          }
                        />
                      ) : (
                        <span className="node-chat-text">{line.text}</span>
                      )}
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
                    {line.steering && (
                      <div className="chat-steering-receipt" role="status">
                        <strong>{line.steering.label}</strong>
                        {line.steering.reason && <span>{line.steering.reason}</span>}
                      </div>
                    )}
                    {line.attachments?.some((attachment) => attachment.reference) && (
                      <div className="chat-input-references">
                        {line.attachments.map((attachment) =>
                          attachment.reference ? (
                            <ReferenceChip
                              key={attachment.attachment_id}
                              projectId={project.id}
                              reference={{
                                ...sourceReference(attachment.reference),
                                label: attachment.name,
                              }}
                              version={
                                attachment.reference.graph_head
                                  ? `r${attachment.reference.graph_head.revision}`
                                  : attachment.reference.version?.slice(0, 7)
                              }
                            />
                          ) : null,
                        )}
                      </div>
                    )}
                    {line.attachments?.map((attachment) => {
                      if (attachment.reference) return null;
                      const expired = Date.parse(attachment.expires_at) <= expiryClock;
                      return (
                        <div
                          className={`chat-input-attachment${expired ? " expired" : ""}`}
                          key={attachment.attachment_id}
                        >
                          <File size={14} />
                          <span>
                            <strong>{attachment.name}</strong>
                            <small>
                              {attachment.media_type} · {formatBytes(attachment.size)}
                            </small>
                          </span>
                          {expired && <em>Expired</em>}
                        </div>
                      );
                    })}
                    {pausedLineTask ? (
                      <InlinePausedTask
                        task={pausedLineTask}
                        disabled={readOnly}
                        onResume={() => onResumeTask(pausedLineTask)}
                        onRetry={() => onRetryTask(pausedLineTask)}
                      />
                    ) : activeLineTask ? (
                      <InlineTaskProgress task={activeLineTask} />
                    ) : failedTurn ? null : pendingLine ? (
                      <InlineTaskProgress task={null} />
                    ) : null}
                  </>
                ) : pausedLineTask ? null : (
                  <span className="node-chat-text">{line.text}</span>
                )}
                {retryLineTask && (
                  <button
                    type="button"
                    className="icon-button chat-error-retry"
                    aria-label="Retry"
                    title="Retry"
                    disabled={readOnly}
                    onClick={() => onRetryTask(retryLineTask)}
                  >
                    <RotateCcw size={14} />
                  </button>
                )}
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
                    return renderArtifactPlayer(
                      line.taskId,
                      artifact.name,
                      artifact,
                      artifact.name,
                    );
                  const taskUpdatedAt =
                    relatedTasks.find((task) => task.operation_id === line.taskId)?.updated_at ??
                    "";
                  const previewFailed = failedArtifactPreviews.has(
                    `${line.taskId}:${artifact.artifact_id}`,
                  );
                  const unavailable = !artifact.available;
                  const unavailableReason =
                    (!artifact.available && artifact.unavailable_reason) ||
                    (previewFailed ? "Preview unavailable" : null);
                  const shellError = artifactShellErrors.get(
                    `${line.taskId}:${artifact.artifact_id}`,
                  );
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
                              src={versionedArtifactContentUrl(
                                project.id,
                                line.taskId,
                                artifact.artifact_id,
                                taskUpdatedAt,
                              )}
                              alt={artifact.name}
                              onError={() =>
                                markArtifactPreviewFailed(line.taskId, artifact.artifact_id)
                              }
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
                          <button
                            type="button"
                            onClick={() => void openArtifact(line.taskId, artifact)}
                          >
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
                              href={artifactUrl(
                                project.id,
                                line.taskId,
                                artifact.artifact_id,
                                "download",
                              )}
                              download={artifact.name}
                            >
                              <Download size={12} /> Download
                            </a>
                          ))}
                        {artifact.can_keep && (
                          <button
                            type="button"
                            data-artifact-action="keep"
                            disabled={keepingArtifacts.has(
                              `${line.taskId}:${artifact.artifact_id}`,
                            )}
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
                })}
                {line.artifactOmissions && (
                  <p className="chat-artifact-omissions" role="status">
                    {line.artifactOmissions.discovery_failed
                      ? "Artifact discovery failed"
                      : `Artifacts omitted: ${Object.entries(line.artifactOmissions)
                          .filter(([, count]) => typeof count === "number" && count > 0)
                          .map(([reason, count]) => `${reason.replaceAll("_", " ")}: ${count}`)
                          .join("; ")}`}
                  </p>
                )}
                {/* A published version shows itself; every outcome still tells new from updated. */}
                {line.artifactEditOutcomes
                  ?.filter((outcome) => outcome.status !== "published")
                  .map((outcome) => (
                    <p key={outcome.artifact_id} className="chat-artifact-omissions" role="status">
                      {artifactEditOutcomeText(
                        outcome,
                        line.artifactEditOutcomes ?? [],
                        line.artifacts ?? [],
                      )}
                    </p>
                  ))}
                {line.role === "agent" && (line.graphUpdate || line.turnOutcome) && (
                  <div className="chat-turn-receipts">
                    {line.turnOutcome && <TurnOutcomeChip outcome={line.turnOutcome} />}
                    {line.graphUpdate && (
                      <GraphUpdateReceipt
                        update={line.graphUpdate}
                        taskId={line.taskId}
                        repairBusy={repairingTaskId === line.taskId}
                        repairDisabled={
                          readOnly ||
                          graphChangesDisabled ||
                          relatedActive ||
                          submitting ||
                          reviewPending
                        }
                        repairContinued={continuedTaskIds.has(line.taskId)}
                        latestReceipt={latestGraphReceiptLineIds.has(line.lineId)}
                        canApplyAgain={
                          relatedTasks.find((task) => task.operation_id === line.taskId)
                            ?.can_apply_again === true
                        }
                        repairError={repairErrors.get(line.taskId) ?? null}
                        onInspectTask={onInspectTask}
                        onOpenInbox={onOpenInbox}
                        onRepair={() => void repairGraphUpdate(line.taskId)}
                        onApplyAgain={() => void repairGraphUpdate(line.taskId, "apply_again")}
                      />
                    )}
                  </div>
                )}
              </div>
              {compaction?.placement === "after" && (
                <CompactionDivider compactions={compaction.compactions} />
              )}
            </Fragment>
          );
        })}
        {/* Open cards end the transcript and scroll with it, so they never
            squeeze the conversation above the composer. */}
        {questionState.error && (
          <div className="node-chat-line error" role="alert">
            {questionState.error}
          </div>
        )}
        {questionState.questions
          .filter((question) => questionIsOpen(question) && !question.withdrawn_readonly)
          .map((question) => (
            <QuestionCard
              key={question.question_id}
              question={question}
              apiBase={questionApiBase}
              onResolved={questionState.refresh}
              continueChat={!questionEpisodeId && question.state === "parked" && !relatedActive}
            />
          ))}
        {submitError && <div className="node-chat-line error">{submitError}</div>}
      </div>
      {!canCompose && readOnlyNotice}
      {canCompose && (
        <div
          className={`chat-composer${composer.dragging ? " is-dragging-files" : ""}`}
          data-mode={mode}
          {...composer.dropTarget(addFiles, insertReferenceText)}
        >
          <SkillPicker {...skills.props} />
          {freshProviderSession && (
            <ProviderSwitchHint provider={readiness?.label || config.provider} />
          )}
          {annotations.length > 0 && (
            <div className="chat-annotation-summary">
              <button
                className={`chat-annotation-count${annotationsOpen ? " is-open" : ""}`}
                type="button"
                aria-expanded={annotationsOpen}
                aria-controls={annotationPanelId}
                onClick={() => setAnnotationsOpen((open) => !open)}
              >
                <MessageCircle size={12} /> {annotations.length} annotation
                {annotations.length === 1 ? "" : "s"}
              </button>
            </div>
          )}
          {annotationsOpen && annotations.length > 0 && (
            <section
              className="chat-annotation-review"
              id={annotationPanelId}
              aria-label="Staged annotations"
            >
              <header>
                <strong>Annotations</strong>
                <button
                  type="button"
                  className="icon-button"
                  aria-label="Close annotations"
                  onClick={() => setAnnotationsOpen(false)}
                >
                  <X size={14} />
                </button>
              </header>
              <div className="chat-annotation-review-list">
                {annotations.map((annotation, index) => (
                  <article key={annotation.id}>
                    <blockquote>{annotation.selectedText}</blockquote>
                    {annotation.artifact && <small>In {annotation.artifact.name}</small>}
                    <textarea
                      aria-label={`Comment for annotation ${index + 1}`}
                      disabled={submitting}
                      maxLength={MAX_CHAT_ANNOTATION_COMMENT_LENGTH}
                      value={annotation.comment}
                      onChange={(event) => updateAnnotation(annotation.id, event.target.value)}
                    />
                    <button
                      type="button"
                      aria-label={`Remove annotation ${index + 1}`}
                      disabled={submitting}
                      onClick={() => removeAnnotation(annotation.id)}
                    >
                      <X size={12} /> Remove
                    </button>
                  </article>
                ))}
              </div>
            </section>
          )}
          <ComposerAttachmentChips composer={composer} />
          <ComposerTextarea
            draft={draft}
            ref={textareaRef}
            aria-label="Message"
            aria-keyshortcuts="Shift+Tab"
            disabled={awaitingSteerReceipt}
            onChange={(event) => {
              setSendAfterDictation(null);
              if (dictating) stopDictation(true);
              updateMessage(event.target.value);
            }}
            onPaste={(event) => composer.paste(event, addFiles, insertReferenceText)}
            onKeyDown={(event) => {
              if (skills.handleKeyDown(event)) return;
              if (!commentOnly && isConversationModeShortcut(event.key, event.shiftKey)) {
                if (presentation !== "workspace") {
                  event.preventDefault();
                  toggleMode();
                }
                return;
              }
              if (returnSends(event)) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div className="chat-send">
            <div className="chat-composer-tools">
              <ComposerAddPicker
                composer={composer}
                disabled={attachDisabled}
                referencesDisabled={commentOnly}
                onAttach={addFiles}
                onPickReference={addReference}
              />
              {!commentOnly && (
                <>
                  <div
                    className="segmented chat-mode-toggle"
                    role="group"
                    aria-label="Conversation mode"
                  >
                    {(["discuss", "work"] as const).map((option) => (
                      <button
                        type="button"
                        className={option}
                        aria-pressed={mode === option}
                        title={MODE_HINTS[option]}
                        disabled={Boolean(steeringTask)}
                        onClick={() => selectMode(option)}
                        key={option}
                      >
                        {modeLabel(option)}
                      </button>
                    ))}
                  </div>
                  <WorktreeControls
                    key={`${project.id}:${chatId}`}
                    state={worktree.state}
                    error={worktree.error}
                    disabled={Boolean(
                      relatedActive ||
                      pausedAttempt ||
                      submitting ||
                      reviewPending ||
                      repairingTaskId,
                    )}
                    onIntegrate={integrateWorktree}
                    onRemove={worktree.remove}
                    onPreviewRemove={worktree.previewRemoval}
                    onRefresh={worktree.refresh}
                  />
                </>
              )}
              <div className="chat-compute-picker chat-options-picker" ref={optionsRef}>
                <button
                  className="chat-compute-trigger"
                  type="button"
                  aria-expanded={optionsOpen}
                  aria-label="Options"
                  title="Options"
                  onClick={() => setOptionsOpen((open) => !open)}
                >
                  <SlidersHorizontal size={14} />
                  {optionCount ? <strong>{optionCount}</strong> : null}
                  <ChevronUp size={12} />
                </button>
                {/* Mounted while folded, so the count reflects the saved Browser choice. */}
                <fieldset
                  className="chat-options-menu"
                  aria-label="Turn options"
                  hidden={!optionsOpen}
                >
                  <ChatBrowserControl
                    key={`${project.id}:${chatId}`}
                    apiBase={questionApiBase}
                    chatId={chatId}
                    machine={config.run_on}
                    disabled={readOnly}
                    onRequestedChange={setBrowserOn}
                  />
                  {!commentOnly && (
                    <WorktreeChooser
                      state={worktree.state}
                      chosen={worktree.chosen}
                      disabled={Boolean(
                        relatedActive ||
                        pausedAttempt ||
                        submitting ||
                        reviewPending ||
                        repairingTaskId,
                      )}
                      onChoose={worktree.choose}
                    />
                  )}
                </fieldset>
              </div>
            </div>
            <div className="chat-send-actions">
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
                disabled={submitting || dictationState === "transcribing"}
                // An unavailable microphone still rereads the choice, then points to Settings.
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
              {dictationSecondsLeft !== null && (
                <span className="chat-dictation-countdown" aria-live="polite">
                  {dictationSecondsLeft}s
                </span>
              )}
              <button
                className="icon-button primary chat-send-button"
                disabled={
                  // While dictating, Send finishes the dictation and sends its transcript.
                  !(draftSendable || artifactContext || dictating) ||
                  !annotationsComplete ||
                  submitting ||
                  (steeringTask
                    ? attachments.length > 0 || references.length > 0 || artifactContext !== null
                    : attachmentsUnready ||
                      relatedActive ||
                      Boolean(pausedAttempt) ||
                      Boolean(repairingTaskId) ||
                      reviewPending ||
                      scope.length === 0 ||
                      !providerReady)
                }
                onClick={() => void send()}
                aria-label={
                  steeringTask ? steeringTask.steer_action_label : `Start ${modeLabel(mode)} turn`
                }
              >
                <Send size={16} />
              </button>
            </div>
            {discardPrompt ? (
              <span className="chat-dictation-error" role="alert">
                Discard the kept dictation?
                <button
                  className="button compact secondary"
                  type="button"
                  onClick={() => {
                    const action = discardPrompt;
                    setDiscardPrompt(null);
                    setKeptSpeech(draftKey, null);
                    void (action === "send" ? send() : toggleDictation());
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
            ) : null}
          </div>
        </div>
      )}
      {selectionComment && !annotationComposer && typeof document !== "undefined"
        ? createPortal(
            <button
              ref={selectionCommentRef}
              type="button"
              className="chat-selection-comment"
              data-mode={mode}
              style={{ left: selectionComment.position.left, top: selectionComment.position.top }}
              disabled={submitting}
              // Keep the selection while pressing, so the staged text is what was shown.
              onPointerDown={(event) => event.preventDefault()}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                const { range } = selectionComment;
                setSelectionComment(null);
                openAnnotationComposer(range);
              }}
            >
              <MessageCirclePlus size={13} /> Comment
            </button>,
            document.body,
          )
        : null}
      {annotationComposer && typeof document !== "undefined"
        ? createPortal(
            <form
              ref={annotationComposerRef}
              className="chat-annotation-composer"
              data-mode={mode}
              aria-label={
                annotationComposer.step === "select" ? "Select answer text" : "Add annotation"
              }
              style={
                {
                  left: annotationComposer.position?.left,
                  top: annotationComposer.position?.top,
                  visibility: annotationComposer.position ? undefined : "hidden",
                  ...(annotationViewport
                    ? {
                        "--chat-annotation-viewport-left": `${annotationViewport.left}px`,
                        "--chat-annotation-viewport-top": `${annotationViewport.top}px`,
                        "--chat-annotation-viewport-width": `${annotationViewport.width}px`,
                        "--chat-annotation-viewport-height": `${annotationViewport.height}px`,
                        "--chat-annotation-viewport-right": `${annotationViewport.right}px`,
                        "--chat-annotation-viewport-bottom": `${annotationViewport.bottom}px`,
                      }
                    : {}),
                } as CSSProperties
              }
              onSubmit={(event) => {
                event.preventDefault();
                if (submitting) return;
                if (annotationComposer.step === "select") continueKeyboardAnnotation();
                else stageAnnotation();
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  dismissAnnotationComposer(true);
                }
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.preventDefault();
                  if (annotationComposer.step === "select") continueKeyboardAnnotation();
                  else stageAnnotation();
                }
              }}
            >
              <header>
                <strong>
                  {annotationComposer.step === "select" ? "Select text" : "Add annotation"}
                </strong>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Cancel annotation"
                  disabled={submitting}
                  onClick={() => dismissAnnotationComposer(true)}
                >
                  <X size={14} />
                </button>
              </header>
              {annotationComposer.step === "select" ? (
                <>
                  <textarea
                    className="chat-annotation-source"
                    ref={annotationSelectionRef}
                    aria-label="Select answer text"
                    disabled={submitting}
                    readOnly
                    defaultValue={annotationComposer.answerText}
                    onSelect={(event) => updateKeyboardAnnotationSelection(event.currentTarget)}
                  />
                  {annotationComposer.selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH && (
                    <strong className="chat-annotation-error" role="alert">
                      Select at most {MAX_CHAT_ANNOTATION_TEXT_LENGTH.toLocaleString()} characters.
                    </strong>
                  )}
                  <button
                    className="button compact primary"
                    type="submit"
                    disabled={
                      submitting ||
                      !annotationComposer.selectedText ||
                      annotationComposer.selectedText.length > MAX_CHAT_ANNOTATION_TEXT_LENGTH
                    }
                  >
                    Comment on selection
                  </button>
                </>
              ) : (
                <>
                  <blockquote>{annotationComposer.selectedText}</blockquote>
                  <textarea
                    ref={annotationCommentRef}
                    aria-label="Comment"
                    disabled={submitting}
                    maxLength={MAX_CHAT_ANNOTATION_COMMENT_LENGTH}
                    value={annotationComment}
                    onChange={(event) => setAnnotationComment(event.target.value)}
                    onKeyDown={(event) => {
                      // Plain Enter only: Shift+Enter inserts a newline and the
                      // form's own modifier+Enter shortcut stages the comment.
                      if (event.key !== "Enter" || event.shiftKey || event.ctrlKey || event.metaKey)
                        return;
                      event.preventDefault();
                      if (submitting || !annotationComment.trim()) return;
                      event.currentTarget.form?.requestSubmit();
                    }}
                  />
                  <button
                    className="button compact primary"
                    type="submit"
                    disabled={submitting || !annotationComment.trim()}
                  >
                    Add comment
                  </button>
                </>
              )}
            </form>,
            document.body,
          )
        : null}
    </div>
  );
}

/** The composer's text field: the only part of the chat a keystroke re-renders. */
function ComposerTextarea({
  draft,
  ...props
}: { draft: ComposerDraft } & Omit<React.ComponentProps<"textarea">, "value">) {
  const value = useComposerDraft(draft);
  return <textarea {...props} value={value} />;
}

function InlineTaskProgress({ task }: { task: AgentTask | null }) {
  const label = task
    ? task.status_message || `${taskKindLabel(task.kind)} is running`
    : "Starting task";
  return (
    <>
      <span className="chat-task-live" role="status" aria-live="polite" aria-atomic="true">
        {label}
      </span>
      <details className="chat-task-activity">
        <summary>
          <LoaderCircle className="spin" size={12} />
          <span>Activity</span>
        </summary>
        <div className="chat-task-inline running">
          <span>{label}</span>
        </div>
      </details>
    </>
  );
}

function InlinePausedTask({
  task,
  disabled,
  onResume,
  onRetry,
}: {
  task: AgentTask;
  disabled: boolean;
  onResume: () => void;
  onRetry: () => void;
}) {
  return (
    <div className="chat-task-inline paused" role="status" aria-label="Agent task paused">
      <span>{task.status_message}</span>
      <button
        type="button"
        className="button compact primary"
        disabled={disabled}
        onClick={onResume}
      >
        <Play size={12} /> Resume
      </button>
      <button
        type="button"
        className="button compact secondary"
        disabled={disabled}
        onClick={onRetry}
      >
        <RotateCcw size={12} /> Retry
      </button>
    </div>
  );
}

function TurnOutcomeChip({ outcome }: { outcome: TurnOutcome }) {
  const chip = turnOutcomeChip(outcome);
  return (
    <span className="chat-turn-outcome" data-outcome={chip.status}>
      {chip.label}
      {chip.reason ? ` · ${chip.reason}` : ""}
    </span>
  );
}

function GraphUpdateReceipt({
  update,
  taskId,
  repairBusy,
  repairDisabled,
  repairContinued,
  latestReceipt,
  canApplyAgain,
  repairError,
  onInspectTask,
  onOpenInbox,
  onRepair,
  onApplyAgain,
}: {
  update: GraphUpdateResult;
  taskId: string;
  repairBusy: boolean;
  repairDisabled: boolean;
  repairContinued: boolean;
  latestReceipt: boolean;
  canApplyAgain: boolean;
  repairError: string | null;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onRepair: () => void;
  onApplyAgain: () => void;
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
            <TriangleAlert size={12} /> Graph update rejected
          </strong>
        )}
        {update.status === "unavailable" && (
          <strong>
            <TriangleAlert size={12} />{" "}
            {update.commit_status === "present"
              ? "Graph update committed, but canonical state became unreachable"
              : update.commit_status === "unknown"
                ? "Graph update may have been committed: canonical state unreachable"
                : "Graph update not applied: canonical state unreachable"}
          </strong>
        )}
        {update.status === "applied" && proposalCount > 0 && (
          <button type="button" onClick={onOpenInbox}>
            <Inbox size={12} />
            {proposalCount} proposal{proposalCount === 1 ? "" : "s"} sent to Inbox
          </button>
        )}
        {latestReceipt && update.status === "rejected" && update.repairable && !repairContinued && (
          <button type="button" disabled={repairBusy || repairDisabled} onClick={onRepair}>
            <RotateCcw className={repairBusy ? "spin" : undefined} size={12} />
            Repair graph update
          </button>
        )}
        {latestReceipt && update.status === "unavailable" && canApplyAgain && (
          <button type="button" disabled={repairBusy || repairDisabled} onClick={onApplyAgain}>
            <RotateCcw className={repairBusy ? "spin" : undefined} size={12} />
            Apply again
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
      {(update.status === "rejected" || update.status === "unavailable") &&
        update.validation_messages.length > 0 && (
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

const ARTIFACT_EDIT_FAILURES: Record<NonNullable<ArtifactEditOutcome["reason"]>, string> = {
  missing: "the file was removed",
  type_changed: "the file type changed",
  too_large: "the file is too large",
  unverified: "the edited file could not be verified",
};

/** Say why an artifact of this session has no new version under this reply. */
function artifactEditOutcomeText(
  outcome: ArtifactEditOutcome,
  outcomes: ArtifactEditOutcome[],
  artifacts: AgentArtifactDescriptor[],
): string {
  if (outcome.status === "forked")
    return "The artifact changed during this turn, so the edit was saved as a separate artifact.";
  if (outcome.status === "failed")
    return `Edit not published: ${outcome.reason ? ARTIFACT_EDIT_FAILURES[outcome.reason] : "it could not be read"}. The current version is kept.`;
  // Only artifacts this turn created; one it updated in place has its own outcome.
  const separate = artifacts.filter(
    (item) => !outcomes.some((other) => other.artifact_id === item.artifact_id),
  );
  return separate.length
    ? `Not edited in place; the current version is kept. ${separate.map((item) => item.name).join(", ")} is a separate artifact.`
    : "Not edited in place; the current version is kept.";
}

function modeLabel(mode: ConversationMode): "Discuss" | "Work" {
  return mode === "discuss" ? "Discuss" : "Work";
}

/** Say what each mode is allowed to do, where the human commits to one.
 *
 * Capability is fixed in code; this only describes it. Discuss holds no graph or
 * filesystem authority, so the difference is worth stating at the control rather
 * than leaving two bare verbs to be learned by consequence.
 */
const MODE_HINTS: Record<ConversationMode, string> = {
  discuss: "Discuss: reads and answers only. Nothing is written to the repository or the graph.",
  work: "Work: may edit files in the run's write roots and propose a graph patch.",
};

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

function chatAnnotationsStorageKey(
  actorId: string | null,
  projectId: string,
  chatId: string,
): string | null {
  return memberDraftKey(
    actorId,
    `chat-annotations:${encodeURIComponent(projectId)}:${encodeURIComponent(chatId)}`,
  );
}

function readStagedChatAnnotations(key: string | null): StagedChatAnnotation[] {
  if (!key) return [];
  try {
    return parseStagedChatAnnotations(sessionStorage.getItem(key));
  } catch {
    return [];
  }
}

function writeSessionStorage(key: string | null, value: string): void {
  if (!key) return;
  try {
    sessionStorage.setItem(key, value);
  } catch {}
}

function removeSessionStorage(key: string | null): void {
  if (!key) return;
  try {
    sessionStorage.removeItem(key);
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
