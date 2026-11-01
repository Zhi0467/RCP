import { AgentWindowTranscript } from "../agentWindow/AgentWindowTranscript";
import {
  useAgentWindowLines,
  type PendingAgentWindowTurn,
} from "../agentWindow/useAgentWindowLines";
import { useAgentWindowArtifacts } from "../agentWindow/useAgentWindowArtifacts";
import { useAgentWindowScroll } from "../agentWindow/useAgentWindowScroll";
import { projectAgentWindowEndpoints } from "../agentWindow/agentWindowEndpoints";
import { ProviderSwitchHint } from "./ProviderSwitchHint";
import { ChatOriginLink } from "./ChatBranchPicker";
import { ChatComposer } from "./ChatComposer";
import { ChatHeaderMenu, type ChatHeaderAction } from "./ChatHeaderMenu";
import type { ChatAgentSetup, ChatOrigin } from "../core/types";
import { memberDraftKey } from "../core/draftStorage";
import { MAIN_GRAPH } from "../core/graphTarget";
import { useComposerAttachments } from "./composerAttachments";
import { referenceDraftKey } from "../core/projectReferences";
import { QuestionCard } from "./QuestionCard";
import { useQuestions } from "./useQuestions";
import { questionIsOpen } from "./questions";
import { ExternalJobRow } from "../experiments/ExternalJobRow";
import {
  ChevronUp,
  LoaderCircle,
  MessageCircle,
  Mic,
  MicOff,
  RadioTower,
  Send,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ApiError,
  loadServiceConnections,
  loadConnectionModels,
  transcribeAudio,
} from "../core/api";
import {
  isActiveTask,
  resolvedChatSessionId,
  relatedChatTasks,
  resumablePausedChatTask,
} from "../agents/agentTasks";
import {
  chatDraftStorageKey,
  chatModeStorageKey,
  isConversationModeShortcut,
  latestPersistedChatConfig,
  originRunConfig,
  latestPersistedConversationMode,
  parseConversationMode,
  startConversationTurn,
  toggleConversationMode,
  type ConversationTurnSubmission,
  type StartConversationTask,
} from "./chatWorkspace";
import { returnSends } from "../ui/composerKeys";
import { assembleChatTurn, replaceTextSpan, stagedArtifactContext } from "./chatInput";
import { AnswerCommentLayers, transcriptSource, useAnswerComments } from "./answerComments";
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
  AgentRunConfig,
  AgentTask,
  ChatMessage,
  ConversationMode,
  GraphNode,
  GraphTargetRef,
  GraphUpdateRecovery,
  ProjectSnapshot,
  ServiceConnection,
  WatcherRecord,
  WorktreeIntegrationOption,
} from "../core/types";
import {
  AgentConfigChip,
  AgentConfigControls,
  launchProviderReady,
  profileRunConfig,
} from "../core/AgentConfigControls";
import { useSkillPicker } from "../core/SkillPicker";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { AgentStatusStrip } from "./AgentStatusStrip";
import { ChatBrowserControl } from "../core/BrowserControls";
import { WorktreeChooser, WorktreeControls, useConversationWorktree } from "./WorktreeControls";
import { worktreeRepositoryChoices } from "./worktreeRepositories";

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

export { pendingTurnShownByHistory } from "../agentWindow/useAgentWindowLines";

interface PendingChatTurn extends PendingAgentWindowTurn {
  /** The start's Idempotency-Key, kept across a Resend. */
  requestId: string;
  /** The composer text it was sent from, which a failed send puts back. */
  draft: string;
  submission: ConversationTurnSubmission;
  submittedOverride: { chatId: string; config: AgentRunConfig } | null;
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
  const {
    transcript,
    statusTasks,
    compactionByLine,
    continuedTaskIds,
    latestGraphReceiptLineIds,
    inlineArtifactRefreshToken,
  } = useAgentWindowLines({ messages: displayedMessages, tasks: relatedTasks, pendingTurn });
  const endpoints = useMemo(
    () => projectAgentWindowEndpoints(project.id, chatId),
    [project.id, chatId],
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
  // A worktree checks out one writable repository on the turn's machine; the
  // human picks it only when several qualify. A bound worktree keeps its own.
  const worktreeRepositories = worktreeRepositoryChoices(project, config.run_on);
  const [worktreeRepositoryPick, setWorktreeRepositoryPick] = useState<{
    chatId: string;
    alias: string;
  } | null>(null);
  const worktreeRepository =
    worktreeRepositoryPick?.chatId === chatId &&
    worktreeRepositories.includes(worktreeRepositoryPick.alias)
      ? worktreeRepositoryPick.alias
      : (worktreeRepositories[0] ?? null);
  const worktree = useConversationWorktree(
    project.id,
    chatId,
    node ? "node" : "project",
    node?.id ?? null,
    config.run_on,
    worktreeRepository ? [worktreeRepository] : [],
    relatedTasks.map((task) => `${task.operation_id}:${task.updated_at}`).join("\0"),
    !readOnly,
    // The conversation's own graph: an orchestrator card opened from main lives on its branch.
    graphTarget,
  );
  const draftKey = chatDraftStorageKey(actorId, project.id, chatId);
  const modeKey = chatModeStorageKey(project.id, chatId);
  const annotationsKey = chatAnnotationsStorageKey(actorId, project.id, chatId);
  const derivedMode = useMemo(
    () => latestPersistedConversationMode(historyMessages, relatedTasks),
    [historyMessages, relatedTasks],
  );
  const [draft] = useState(() => createComposerDraft(readStorage(draftKey) ?? ""));
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const chatLinesRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const comments = useAnswerComments({
    storageKey: annotationsKey,
    enabled: !readOnly,
    busy: submitting,
    sources: [transcriptSource(chatLinesRef)],
    message: textareaRef,
    onError: setSubmitError,
  });
  const { annotations } = comments;
  const artifactContext = useMemo(() => stagedArtifactContext(annotations), [annotations]);
  const canCompose = !readOnly || (allowArtifactComments && Boolean(artifactContext));
  // A read-only chat composes only artifact comments, and those edit in Discuss.
  const commentOnly = readOnly && Boolean(artifactContext);
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
  const referencesKey = referenceDraftKey(
    actorId,
    project.id,
    project.graph_target ?? MAIN_GRAPH,
    chatId,
  );
  const composer = useComposerAttachments({
    project,
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
    referencesDisabled: submitting || commentOnly,
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
  // Send pressed while dictating: send once the final transcript is in the draft,
  // to the target Send addressed when pressed (a steered attempt, or a new turn).
  const [sendAfterDictation, setSendAfterDictation] = useState<{
    target: string;
  } | null>(null);
  const dictationSpanRef = useRef<DictationSpan | null>(null);
  const dictationTimerRef = useRef<number | null>(null);
  const microphoneRef = useRef<{ sessionId: string; claim: MicrophoneClaim } | null>(null);
  const networkDictationRef = useRef<NetworkDictation | null>(null);
  const { shouldStickToBottomRef, restoreTopRef, handleChatScroll } = useAgentWindowScroll(
    endpoints.scopeKey,
    chatLinesRef,
    transcript,
    questionState.questions,
  );
  const [repairingTaskId, setRepairingTaskId] = useState<string | null>(null);
  const [repairErrors, setRepairErrors] = useState<Map<string, string>>(() => new Map());
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
  const pausedAttempt = resumablePausedChatTask(relatedTasks);
  const sessionId = resolvedChatSessionId(relatedTasks);
  // A follow-up joins the running turn, so the composer shows that turn's mode.
  const steeringMode = steeringTask ? parseConversationMode(steeringTask.request.mode) : null;
  const mode = commentOnly ? "discuss" : (steeringMode ?? modeState.value);
  modeRef.current = mode;
  // A Work turn also needs the provider's Work probe; Discuss does not.
  const providerReady = launchProviderReady(project, config, mode === "work", Boolean(sessionId));
  const chatTitle = node?.title || conversationTitle || project.name;
  const attachDisabled = composer.full || composer.preparing || submitting || awaitingSteerReceipt;
  const attachmentsUnready = composer.unready;
  const annotationsComplete = comments.complete;
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

  const steer = async (task: AgentTask) => {
    const draftMessage = draft.get();
    const draftAnnotations = annotations;
    const text = assembleChatTurn(draftMessage, annotations);
    if (!comments.requireComplete()) return;
    // Files and artifact selections are staged for a new turn; a steer carries text only.
    if (!text || composer.staged || artifactContext || submitting || !task.steer_turn_id) return;
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
      // the delivered draft is exactly what is consumed. Artifact comments can
      // still arrive from another view meanwhile, and they survive.
      draft.set((current) => (current === draftMessage ? "" : current));
      comments.clear(draftAnnotations);
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
      worktreeRepository,
      artifactContext,
      composer.turnFields(),
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
    if (!comments.requireComplete()) return;
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
    composer.closeMenu();
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
        nodeId: node?.id ?? null,
        message: text,
        chatId,
        sessionId,
        mode,
        artifactContext,
        attachments: composer.turnFields(),
        skills: skills.selection,
        providerSkillNames: skills.providerSkillNames,
        worktree: mode === "work" && worktree.chosen,
        worktreeRepository,
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
      comments.clear();
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

  const artifacts = useAgentWindowArtifacts({
    endpoints,
    chatId,
    relatedTasks,
    transcript,
    comments,
    onRefreshTask,
    setSubmitError,
    inlineArtifactRefreshToken,
    readOnly,
    allowArtifactComments,
    repositories: project.repositories,
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
        <AgentWindowTranscript
          transcript={transcript}
          tasks={relatedTasks}
          questions={questionState.questions}
          endpoints={endpoints}
          artifacts={artifacts}
          comments={comments}
          compactionByLine={compactionByLine}
          continuedTaskIds={continuedTaskIds}
          latestGraphReceiptLineIds={latestGraphReceiptLineIds}
          renderQuestion={(question) => (
            <QuestionCard
              question={question}
              apiBase={questionApiBase}
              onResolved={questionState.refresh}
            />
          )}
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
          onRetryTask={onRetryTask}
          onInspectTask={onInspectTask}
          onOpenInbox={onOpenInbox}
          onRepairGraphUpdate={repairGraphUpdate}
        />
        {/* Open cards end the transcript and scroll with it, so they never
            squeeze the conversation above the composer. */}
        {questionState.error && (
          <div className="node-chat-line error" role="alert">
            {questionState.error}
          </div>
        )}
        {questionState.questions
          .filter(
            (question) =>
              questionIsOpen(question) && !question.card && !question.withdrawn_readonly,
          )
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
        <ChatComposer
          hint={
            <ProviderSwitchHint
              project={project}
              config={config}
              tasks={relatedTasks}
              sessionId={sessionId}
            />
          }
          mode={mode}
          composer={composer}
          comments={comments}
          skills={skills.props}
          addDisabled={attachDisabled}
          field={
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
              onPaste={composer.paste}
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
          }
          tools={
            <>
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
                      repositories={worktreeRepositories}
                      repository={worktreeRepository}
                      onRepository={(alias) => setWorktreeRepositoryPick({ chatId, alias })}
                    />
                  )}
                </fieldset>
              </div>
            </>
          }
          actions={
            <>
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
                    ? composer.staged || artifactContext !== null
                    : attachmentsUnready ||
                      relatedActive ||
                      Boolean(pausedAttempt) ||
                      Boolean(repairingTaskId) ||
                      reviewPending ||
                      !providerReady)
                }
                onClick={() => void send()}
                aria-label={
                  steeringTask ? steeringTask.steer_action_label : `Start ${modeLabel(mode)} turn`
                }
              >
                <Send size={16} />
              </button>
            </>
          }
          notice={
            discardPrompt ? (
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
            ) : null
          }
        />
      )}
      <AnswerCommentLayers comments={comments} mode={mode} />
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
