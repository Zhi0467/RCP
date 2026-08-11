import {
  AlertTriangle,
  ArrowLeft,
  CircleArrowUp,
  CloudUpload,
  ChevronDown,
  ChevronUp,
  FileText,
  FlaskConical,
  FolderLock,
  GitBranch,
  History,
  Inbox,
  LayoutList,
  LoaderCircle,
  MessageCircle,
  Network,
  RefreshCw,
  RotateCcw,
  Settings2,
  X,
} from "lucide-react";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  isActiveTask,
  parseDismissedTaskIds,
  projectActivityTask,
  serializeDismissedTaskIds,
  taskNotificationStorageKey,
} from "./agentTasks";
import {
  chatIdForTask,
  chatIndicator,
  chatEntryConversationId,
  groupChatConversations,
  latestConversation,
  newlyUnreadChatTaskIds,
  type ChatKind,
  type DraftConversation,
} from "./chatWorkspace";
import {
  api,
  ApiError,
  loadProjectReadiness,
  pinApiInstance,
  registerIdentityNameRequiredHandler,
  registerMutationFailureHandler,
} from "./api";
import {
  loadChatSummaryPage,
  loadChatTranscript,
  mergeChatSummaryPage,
  nextChatSummaryOffset,
  reconcileChatSelectionAfterRefresh,
} from "./chatApi";
import {
  acceptCurrentBackendIdentity,
  applyDesktopUpdate,
  BACKEND_IDENTITY_EVENT,
  backendReconnectLabel,
  checkDesktopUpdate,
  DESKTOP_FOLDER_ACCESS_ACK_KEY,
  desktopShowReady,
  desktopFolderAccessAcknowledgementValue,
  desktopReconnectBackend,
  setDesktopWebviewZoom,
  establishBackendIdentity,
  isDesktopRuntime,
  listenDesktopEvent,
  needsDesktopFolderAccessAcknowledgement,
  reverifyBackendIdentity,
  verifyIdentityAfterMutationFailure,
  type BackendIdentityEventDetail,
  type DesktopUpdate,
} from "./desktopRuntime";
import { graphMutationsDisabled, replayFailureLabel, taskMayMutateGraph } from "./graphAuthority";
import { buildGlossaryIndex } from "./glossary";
import {
  experimentBoardHref,
  parseProjectHash,
  projectHashAfterViewChange,
} from "./experimentBoard";
import { nodeDetailSizeStorageKey, type DetailWindowSlot } from "./floatingWindow";
import type { DagViewport } from "./hooks/dagZoom";
import { AgentTaskInspector } from "./components/AgentTaskInspector";
import { AttentionRail, ProposalJudgmentSection } from "./components/AttentionRail";
import { DetailDrawer } from "./components/DetailDrawer";
import { DraggableWindow } from "./components/DraggableWindow";
import { ProjectHistoryDrawer } from "./components/ProjectHistoryDrawer";
import { ProjectDock } from "./components/ProjectDock";
import { RunDialog } from "./components/RunDialog";
import {
  applyHumanDraft,
  deserializeHumanDraft,
  draftNodeIsBehind,
  emptyHumanDraft,
  humanDraftBehindCount,
  humanDraftChangeCount,
  humanDraftCommittableCount,
  humanDraftOntologyIsStale,
  humanDraftStorageKey,
  humanSyncFailure,
  normalizeHumanDraft,
  retainBehindDraftAfterSync,
  serializeHumanDraft,
  stageDecisionChoice,
  stageNodeEdit,
  stageNodeEditStart,
  stageNodeRemoval,
  stageNodeStanding,
  stageProposalDecision,
  stageCustomNode,
  unstageCustomNode,
  unstageNodeRemoval,
  toHumanSyncRequest,
  type HumanDraft,
} from "./humanDraft";
import type {
  AgentRunConfig,
  AgentTask,
  AgentTaskKind,
  AgentTaskRequest,
  AgentUsageSnapshot,
  AppView,
  ChatSummary,
  ChatTranscript,
  ExperimentControlState,
  ExperimentLoopIndexEntry,
  GraphNode,
  GraphState,
  Health,
  IdentityResponse,
  PaperSnapshot,
  ProjectCard,
  ProjectSnapshot,
  RevisionSummary,
  TrustView,
  ValidationMessage,
  WatcherRecord,
} from "./types";
import { DISPLAY_NAME_MAX_LENGTH } from "./types";
import { ProjectLanding } from "./views/ProjectLanding";
import { ProjectOverview } from "./views/ProjectOverview";
import { ProjectSetup } from "./views/ProjectSetup";
import {
  changeTextScale,
  normalizeTextScale,
  TEXT_SCALE_STORAGE_KEY,
  textScaleShortcut,
  type TextScaleAction,
} from "./textScale";
import { NOTICE_TIMEOUT_MS } from "./uiConstants";
import {
  adjacentProjectTabId,
  closeProjectTab,
  initialProjectHash,
  isEditableShortcutTarget,
  openProjectTab,
  projectViewportRef,
  projectTabShortcut,
  type ProjectTab,
  type ProjectViewState,
} from "./projectTabs";

const PROVIDER_SKILL_READINESS_POLL_DELAY_MS = 1_000;
const PROVIDER_SKILL_READINESS_MAX_FOLLOW_UPS = 20;
const EXPERIMENT_BOARD_POLL_DELAY_MS = 5_000;
export const OPEN_PROJECT_HEARTBEAT_INTERVAL_MS = 3_000;
export const ACTIVE_PROJECT_CACHE_OBSERVE_INTERVAL_MS = 1_000;

export function shouldPollProviderSkillReadiness(
  inventories: ProjectSnapshot["provider_skill_inventories"] | undefined,
  completedFollowUps: number,
): boolean {
  return (
    inventories !== undefined &&
    completedFollowUps < PROVIDER_SKILL_READINESS_MAX_FOLLOW_UPS &&
    Object.values(inventories).some((providers) =>
      Object.values(providers).some((inventory) => inventory?.status === "refreshing"),
    )
  );
}

const AttentionOverview = lazy(() =>
  import("./views/GraphViews").then((module) => ({ default: module.AttentionOverview })),
);
const DagView = lazy(() =>
  import("./views/GraphViews").then((module) => ({ default: module.DagView })),
);
const ExecutionView = lazy(() =>
  import("./views/GraphViews").then((module) => ({ default: module.ExecutionView })),
);
const ScientificView = lazy(() =>
  import("./views/GraphViews").then((module) => ({ default: module.ScientificView })),
);
const PaperWorkspace = lazy(() =>
  import("./views/PaperWorkspace").then((module) => ({ default: module.PaperWorkspace })),
);
const ProjectSettings = lazy(() =>
  import("./views/ProjectSettings").then((module) => ({ default: module.ProjectSettings })),
);
const ChatsWorkspace = lazy(() =>
  import("./views/ChatsWorkspace").then((module) => ({ default: module.ChatsWorkspace })),
);
const NodeChat = lazy(() =>
  import("./components/NodeChat").then((module) => ({ default: module.NodeChat })),
);

const emptyGraph: GraphState = {
  revision: 0,
  nodes: {},
  edges: {},
  proposals: {},
  ambiguities: {},
  glossary: {},
  validation_messages: [],
  belief_transitions: [],
  replay_status: "complete",
  replay_failure: null,
  ontology: { types: [], fields: [], relations: [] },
};

const navItems: Array<{ view: AppView; label: string; icon: React.ReactNode }> = [
  { view: "overview", label: "Overview", icon: <LayoutList size={14} /> },
  { view: "attention", label: "Inbox", icon: <Inbox size={14} /> },
  { view: "scientific", label: "Research", icon: <GitBranch size={14} /> },
  { view: "execution", label: "Runs", icon: <FlaskConical size={14} /> },
  { view: "paper", label: "Paper", icon: <FileText size={14} /> },
  { view: "settings", label: "Settings", icon: <Settings2 size={14} /> },
  { view: "chats", label: "Chats", icon: <MessageCircle size={14} /> },
];

export function revisionSummariesUrl(apiBase: string, revision?: number): string {
  const path = `${apiBase}/history/summaries`;
  return revision === undefined
    ? path
    : `${path}?from_revision=${revision}&to_revision=${revision}`;
}

export async function loadCanonicalRevision(
  fetchJson: <T>(path: string) => Promise<T>,
  apiBase: string,
): Promise<number> {
  const snapshot = await fetchJson<{ revision: number }>(`${apiBase}/cached/revision`);
  return snapshot.revision;
}

export function canonicalRevisionNeedsReload(
  observedRevision: number,
  renderedRevision: number,
): boolean {
  return observedRevision > renderedRevision;
}

export function relatedNodeWindowAction(
  sourceSlot: DetailWindowSlot,
  targetNodeId: string,
  originalNodeId: string | null,
  companionNodeId: string | null,
): { kind: "focus" | "open"; slot: DetailWindowSlot } {
  if (targetNodeId === originalNodeId) return { kind: "focus", slot: "original" };
  if (targetNodeId === companionNodeId) return { kind: "focus", slot: "companion" };
  return { kind: "open", slot: sourceSlot === "original" ? "companion" : "original" };
}

function pageIsHidden(): boolean {
  return document.visibilityState === "hidden";
}

export function AcceptanceAgentIndicator({
  agentMode,
}: {
  agentMode: Health["agent_mode"] | null | undefined;
}) {
  if (agentMode !== "acceptance") return null;
  return (
    <aside className="acceptance-agent-indicator" role="status" aria-live="polite">
      <strong>Fake acceptance agent active</strong>
      <span>Acceptance mode · no real provider calls</span>
    </aside>
  );
}

export function terminalTaskNeedsAuthoritativeProjectReload(task: AgentTask): boolean {
  return Boolean(task.applied_revision) || task.request.patch_kind === "experiment_loop";
}

export function shouldShowCoverageBoundaryWarning(
  project: Pick<ProjectSnapshot, "coverage" | "last_refresh_at">,
): boolean {
  return (
    (project.coverage.sessions_skipped.length > 0 ||
      project.coverage.repositories_never_seen.length > 0) &&
    (!project.last_refresh_at || project.coverage.note !== "No seed has completed.")
  );
}

export function failedTaskActionNeedsAuthoritativeProjectReload(
  task: AgentTask,
  action: "pause" | "resume" | "retry",
): boolean {
  return task.request.patch_kind === "experiment_loop" && action !== "pause";
}

export function humanAttentionBlockers(
  canonicalNodes: GraphNode[],
  presentedNodes?: GraphState["nodes"],
): GraphNode[] {
  return canonicalNodes
    .filter(
      (node) => node.type === "blocker" && node.status === "open" && node.standing === "asserted",
    )
    .map((node) => presentedNodes?.[node.id] ?? node);
}

export function decisionsAwaitingChoice(
  canonicalNodes: GraphNode[],
  presentedNodes?: GraphState["nodes"],
): GraphNode[] {
  return canonicalNodes
    .filter(
      (node) => node.type === "decision" && (node.status === "ready" || node.status === "revisit"),
    )
    .map((node) => ({ ...(presentedNodes?.[node.id] ?? node), status: node.status }));
}

export async function loadExperimentWatcherPoll(
  fetchJson: <T>(path: string) => Promise<T>,
  base: string,
): Promise<{
  watchers: WatcherRecord[];
  tasks: AgentTask[];
  project: ProjectSnapshot;
}> {
  const [watchers, tasks, project] = await Promise.all([
    fetchJson<WatcherRecord[]>(`${base}/watchers`),
    fetchJson<AgentTask[]>(`${base}/tasks`),
    fetchJson<ProjectSnapshot>(base),
  ]);
  return { watchers, tasks, project };
}

const PROJECT_HEADER_COLLAPSED_KEY = "rcp:project-header-collapsed";
export const PROJECT_TAB_CACHE_LIMIT = 8;

type ProjectReconciliation = "opening" | "reconciling" | "authoritative" | "failed";

interface CachedProjectTabState {
  project: ProjectSnapshot;
  viewState: ProjectViewState;
  projectHeaderCollapsed: boolean;
  runScope: string[];
  selectedNodeId: string | null;
  companionNodeId: string | null;
  detailFocusTokens: Record<DetailWindowSlot, number>;
  selectedExperimentRunId: string | null;
  focusExperimentRunId: string | null;
  dockedNodeIds: string[];
  floatingChat: { chatId: string; nodeId: string } | null;
  draftConversations: DraftConversation[];
  selectedChatId: string | null;
  unreadChatTaskIds: Set<string>;
  chatSummaries: ChatSummary[];
  chatSummaryTotal: number;
  chatSummaryNextOffset: number;
  chatTranscripts: Map<string, ChatTranscript>;
  selectedCanonicalChat: ChatSummary | null;
  chatTaskStatuses: Map<string, AgentTask["status"]>;
  dagRelationFocusId: string | null;
  retryTask: AgentTask | null;
  humanDraft: HumanDraft | null;
  tasks: AgentTask[];
  latestRevisionSummary: RevisionSummary | null;
  historyRevisionSummaries: RevisionSummary[];
  historySummariesRevision: number | null;
  historySummariesError: string | null;
  projectHistoryOpen: boolean;
  usage: AgentUsageSnapshot | null;
  watchers: WatcherRecord[];
  taskInspectorId: string | null;
  inspectedTask: AgentTask | null;
  activityTaskId: string | null;
  dismissedTaskIds: Set<string>;
  dismissedHistoryNoticeIds: Set<string>;
}

export function cacheProjectTabState<T>(
  cache: Map<string, T>,
  projectId: string,
  state: T,
  limit = PROJECT_TAB_CACHE_LIMIT,
): void {
  cache.delete(projectId);
  cache.set(projectId, state);
  while (cache.size > limit) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

export function cachedSnapshotCanReplace(
  renderedProjectId: string | null,
  renderedRevision: number,
  snapshot: ProjectSnapshot,
): boolean {
  return snapshot.id !== renderedProjectId || snapshot.graph.revision >= renderedRevision;
}

export function projectTabStateForOpen<T>(
  cache: Map<string, T>,
  projectId: string,
): { state: T; loading: false } | null {
  const state = cache.get(projectId);
  if (!state) return null;
  cacheProjectTabState(cache, projectId, state);
  return { state, loading: false };
}

export function projectIdsForCacheHeartbeat(tabs: ProjectTab[]): string[] {
  return [...new Set(tabs.map((tab) => tab.id))];
}

export function inactiveProjectTabState<T>(
  cache: Map<string, T>,
  tabs: ProjectTab[],
  activeProjectId: string | null,
  requestedProjectId: string,
): T | null {
  if (activeProjectId === requestedProjectId || !tabs.some((tab) => tab.id === requestedProjectId))
    return null;
  return cache.get(requestedProjectId) ?? null;
}

export function reconcileInactiveProjectTabState(
  state: CachedProjectTabState,
  snapshot: ProjectSnapshot,
): CachedProjectTabState {
  if (
    !cachedSnapshotCanReplace(state.project.id, state.project.graph.revision, snapshot) ||
    snapshot.id !== state.project.id
  )
    return state;
  const rebased = state.humanDraft ? normalizeHumanDraft(state.humanDraft, snapshot.graph) : null;
  return {
    ...state,
    project: snapshot,
    humanDraft: rebased && humanDraftChangeCount(rebased) > 0 ? rebased : null,
  };
}

export function persistProjectHumanDraft(
  storage: Pick<Storage, "setItem" | "removeItem">,
  projectId: string,
  draft: HumanDraft | null,
): void {
  if (draft && humanDraftChangeCount(draft) > 0) {
    storage.setItem(humanDraftStorageKey(projectId), serializeHumanDraft(draft));
  } else {
    storage.removeItem(humanDraftStorageKey(projectId));
  }
}

interface ProjectCachePollingClock {
  setInterval(callback: () => void, delay: number): number;
  clearInterval(intervalId: number): void;
}

interface ProjectCachePollingVisibility {
  isHidden(): boolean;
  listen(callback: () => void): () => void;
}

export function startProjectCachePolling(
  clock: ProjectCachePollingClock,
  visibility: ProjectCachePollingVisibility,
  sweepOpenProjects: () => void,
  observeActiveProject: () => void,
): () => void {
  const runWhenVisible = (callback: () => void) => () => {
    if (!visibility.isHidden()) callback();
  };
  const sweepInterval = clock.setInterval(
    runWhenVisible(sweepOpenProjects),
    OPEN_PROJECT_HEARTBEAT_INTERVAL_MS,
  );
  const activeInterval = clock.setInterval(
    runWhenVisible(observeActiveProject),
    ACTIVE_PROJECT_CACHE_OBSERVE_INTERVAL_MS,
  );
  const stopListening = visibility.listen(runWhenVisible(sweepOpenProjects));
  return () => {
    clock.clearInterval(sweepInterval);
    clock.clearInterval(activeInterval);
    stopListening();
  };
}

export function singleFlightProjectCacheHeartbeat(
  inFlight: Map<string, Promise<void>>,
  projectId: string,
  heartbeat: () => Promise<void>,
): Promise<void> {
  const pending = inFlight.get(projectId);
  if (pending) return pending;
  const request = heartbeat().finally(() => {
    if (inFlight.get(projectId) === request) inFlight.delete(projectId);
  });
  inFlight.set(projectId, request);
  return request;
}

export default function App() {
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const [initialRoute] = useState(() => {
    const navigation = window.performance.getEntriesByType("navigation")[0] as
      PerformanceNavigationTiming | undefined;
    const hash = initialProjectHash(window.location.hash, navigation?.type);
    if (hash !== window.location.hash) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    }
    return { project: parseProjectHash(hash), setupOpen: hash === "#/projects/new" };
  });
  const [identityReady, setIdentityReady] = useState(false);
  const [identityIssue, setIdentityIssue] = useState<string | null>(null);
  const [verifiedHealth, setVerifiedHealth] = useState<Health | null>(null);
  const [actorIdentity, setActorIdentity] = useState<IdentityResponse | null>(null);
  const [actorIdentityError, setActorIdentityError] = useState<string | null>(null);
  const [actorNamePromptOpen, setActorNamePromptOpen] = useState(false);
  const [actorNameDraft, setActorNameDraft] = useState("");
  const [actorNameSaving, setActorNameSaving] = useState(false);
  const [actorNameError, setActorNameError] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [desktopUpdate, setDesktopUpdate] = useState<DesktopUpdate | null>(null);
  const [updateExpanded, setUpdateExpanded] = useState(false);
  const [updateApplying, setUpdateApplying] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [pendingDesktopProject, setPendingDesktopProject] = useState<{
    projectId: string;
    experimentId: string | null;
  } | null>(null);
  const [desktopAccessError, setDesktopAccessError] = useState<string | null>(null);
  const [projectId, setProjectId] = useState<string | null>(initialRoute.project.projectId);
  const [setupOpen, setSetupOpen] = useState(initialRoute.setupOpen);
  const [projects, setProjects] = useState<ProjectCard[]>([]);
  const [openProjectTabs, setOpenProjectTabs] = useState<ProjectTab[]>([]);
  const [experimentLoops, setExperimentLoops] = useState<ExperimentLoopIndexEntry[]>([]);
  const [project, setProject] = useState<ProjectSnapshot | null>(null);
  const [projectHeaderCollapsed, setProjectHeaderCollapsed] = useState(() =>
    readProjectHeaderCollapsed(projectId),
  );
  const [graph, setGraph] = useState<GraphState>(emptyGraph);
  const [paper, setPaper] = useState<PaperSnapshot | null>(null);
  const [view, setView] = useState<AppView>(initialRoute.project.view);
  const [trustView, setTrustView] = useState<TrustView>(readTrustView);
  const [runScope, setRunScope] = useState<string[]>([]);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [companionNode, setCompanionNode] = useState<GraphNode | null>(null);
  const [detailFocusTokens, setDetailFocusTokens] = useState<Record<DetailWindowSlot, number>>({
    original: 0,
    companion: 0,
  });
  const [selectedExperimentRunId, setSelectedExperimentRunId] = useState<string | null>(
    initialRoute.project.experimentId,
  );
  const [focusExperimentRunId, setFocusExperimentRunId] = useState<string | null>(
    initialRoute.project.experimentId,
  );
  const [experimentStopId, setExperimentStopId] = useState<string | null>(null);
  const [dockedNodeIds, setDockedNodeIds] = useState<string[]>([]);
  const [floatingChat, setFloatingChat] = useState<{ chatId: string; nodeId: string } | null>(null);
  const [draftConversations, setDraftConversations] = useState<DraftConversation[]>([]);
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
  const [unreadChatTaskIds, setUnreadChatTaskIds] = useState<Set<string>>(() => new Set());
  const [chatSummaries, setChatSummaries] = useState<ChatSummary[]>([]);
  const [chatSummaryTotal, setChatSummaryTotal] = useState(0);
  const [chatSummaryNextOffset, setChatSummaryNextOffset] = useState(0);
  const [chatSummariesLoading, setChatSummariesLoading] = useState(false);
  const [chatTranscripts, setChatTranscripts] = useState<Map<string, ChatTranscript>>(
    () => new Map(),
  );
  const [selectedCanonicalChat, setSelectedCanonicalChat] = useState<ChatSummary | null>(null);
  const [dagRelationFocusId, setDagRelationFocusId] = useState<string | null>(null);
  const [textScale, setTextScale] = useState(readTextScale);
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [retryTask, setRetryTask] = useState<AgentTask | null>(null);
  const [loading, setLoading] = useState(true);
  const [projectReconciliation, setProjectReconciliation] =
    useState<ProjectReconciliation>("opening");
  const [humanDraft, setHumanDraft] = useState<HumanDraft | null>(null);
  const [syncingDraft, setSyncingDraft] = useState(false);
  const [taskStarting, setTaskStarting] = useState(false);
  const [taskActionId, setTaskActionId] = useState<string | null>(null);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [latestRevisionSummary, setLatestRevisionSummary] = useState<RevisionSummary | null>(null);
  const [historyRevisionSummaries, setHistoryRevisionSummaries] = useState<RevisionSummary[]>([]);
  const [historySummariesRevision, setHistorySummariesRevision] = useState<number | null>(null);
  const [historySummariesError, setHistorySummariesError] = useState<string | null>(null);
  const [projectHistoryOpen, setProjectHistoryOpen] = useState(false);
  const [usage, setUsage] = useState<AgentUsageSnapshot | null>(null);
  const [watchers, setWatchers] = useState<WatcherRecord[]>([]);
  const [taskInspectorId, setTaskInspectorId] = useState<string | null>(null);
  const [inspectedTask, setInspectedTask] = useState<AgentTask | null>(null);
  const [taskInspectorLoading, setTaskInspectorLoading] = useState(false);
  const [activityTaskId, setActivityTaskId] = useState<string | null>(null);
  const [dismissedTaskIds, setDismissedTaskIds] = useState<Set<string>>(() =>
    readDismissedTaskIds(projectId),
  );
  const [dismissedHistoryNoticeIds, setDismissedHistoryNoticeIds] = useState<Set<string>>(() =>
    readDismissedHistoryNoticeIds(projectId),
  );
  const taskStartLock = useRef(false);
  const activeProjectId = useRef(projectId);
  const authoritativeProjectId = useRef<string | null>(null);
  const reloadRef = useRef<(includeTasks?: boolean) => Promise<void>>(async () => undefined);
  const authoritativeReloadInFlight = useRef<{
    projectId: string;
    request: Promise<void>;
  } | null>(null);
  const projectCacheHeartbeatInFlight = useRef(new Map<string, Promise<void>>());
  const actorIdentityRef = useRef<IdentityResponse | null>(null);
  const actorNamePromptResolver = useRef<((saved: boolean) => void) | null>(null);
  const renderedRevisionRef = useRef(graph.revision);
  const verifiedHealthRef = useRef<Health | null>(null);
  const initialShowHandshake = useRef(false);
  const chatTaskStatuses = useRef<Map<string, AgentTask["status"]>>(new Map());
  const chatSummariesRef = useRef<ChatSummary[]>([]);
  const selectedChatIdRef = useRef<string | null>(null);
  const selectedCanonicalChatRef = useRef<ChatSummary | null>(null);
  const chatSummaryRefreshGeneration = useRef(0);
  const readinessRequestedProjectIds = useRef(new Set<string>());
  const providerSkillReadinessPoll = useRef<{ projectId: string; timeoutId: number } | null>(null);
  const panelRef = useRef<HTMLElement>(null);
  const panelScrollRef = useRef(new Map<AppView, number>());
  const viewRef = useRef<AppView>(view);
  const researchSubviewRef = useRef<AppView>("scientific");
  const dagViewportRefsRef = useRef(new Map<string, { current: DagViewport | null }>());
  const openProjectTabsRef = useRef(openProjectTabs);
  const projectTabStatesRef = useRef(new Map<string, CachedProjectTabState>());
  const currentProjectStateRef = useRef<Omit<CachedProjectTabState, "viewState"> | null>(null);
  openProjectTabsRef.current = openProjectTabs;
  activeProjectId.current = projectId;
  actorIdentityRef.current = actorIdentity;
  renderedRevisionRef.current = graph.revision;
  const [notice, setNotice] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const apiBase = projectId ? `/api/projects/${encodeURIComponent(projectId)}` : "";
  const activeDagViewportRef = projectId
    ? projectViewportRef(dagViewportRefsRef.current, projectId)
    : null;

  currentProjectStateRef.current = project
    ? {
        project,
        projectHeaderCollapsed,
        runScope,
        selectedNodeId: selectedNode?.id ?? null,
        companionNodeId: companionNode?.id ?? null,
        detailFocusTokens,
        selectedExperimentRunId,
        focusExperimentRunId,
        dockedNodeIds,
        floatingChat,
        draftConversations,
        selectedChatId,
        unreadChatTaskIds,
        chatSummaries,
        chatSummaryTotal,
        chatSummaryNextOffset,
        chatTranscripts,
        selectedCanonicalChat,
        chatTaskStatuses: chatTaskStatuses.current,
        dagRelationFocusId,
        retryTask,
        humanDraft,
        tasks,
        latestRevisionSummary,
        historyRevisionSummaries,
        historySummariesRevision,
        historySummariesError,
        projectHistoryOpen,
        usage,
        watchers,
        taskInspectorId,
        inspectedTask,
        activityTaskId,
        dismissedTaskIds,
        dismissedHistoryNoticeIds,
      }
    : null;

  const rememberProjectState = useCallback((id: string | null) => {
    if (!id) return;
    const current = currentProjectStateRef.current;
    if (!current || current.project.id !== id) return;
    const panelScroll = new Map(panelScrollRef.current);
    const dagViewport = dagViewportRefsRef.current.get(id)?.current ?? null;
    if (panelRef.current) panelScroll.set(viewRef.current, panelRef.current.scrollTop);
    cacheProjectTabState(projectTabStatesRef.current, id, {
      ...current,
      runScope: [...current.runScope],
      detailFocusTokens: { ...current.detailFocusTokens },
      dockedNodeIds: [...current.dockedNodeIds],
      floatingChat: current.floatingChat ? { ...current.floatingChat } : null,
      draftConversations: [...current.draftConversations],
      unreadChatTaskIds: new Set(current.unreadChatTaskIds),
      chatSummaries: [...current.chatSummaries],
      chatTranscripts: new Map(current.chatTranscripts),
      chatTaskStatuses: new Map(current.chatTaskStatuses),
      tasks: [...current.tasks],
      historyRevisionSummaries: [...current.historyRevisionSummaries],
      watchers: [...current.watchers],
      dismissedTaskIds: new Set(current.dismissedTaskIds),
      dismissedHistoryNoticeIds: new Set(current.dismissedHistoryNoticeIds),
      viewState: {
        view: viewRef.current,
        panelScroll: [...panelScroll.entries()],
        researchSubview: researchSubviewRef.current,
        dagViewport: dagViewport ? { ...dagViewport } : null,
      },
    });
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timeoutId = window.setTimeout(() => {
      setNotice((current) => (current === notice ? null : current));
    }, NOTICE_TIMEOUT_MS);
    return () => window.clearTimeout(timeoutId);
  }, [notice]);

  // Reading the offset while the outgoing view is still mounted is the only moment it is still true.
  const changeView = useCallback((next: AppView) => {
    const panel = panelRef.current;
    if (panel) panelScrollRef.current.set(viewRef.current, panel.scrollTop);
    const replacementHash = projectHashAfterViewChange(window.location.hash, next);
    if (replacementHash) window.history.replaceState(null, "", replacementHash);
    setView(next);
  }, []);

  const selectChat = useCallback((chatId: string | null) => {
    selectedChatIdRef.current = chatId;
    setSelectedChatId(chatId);
    if (selectedCanonicalChatRef.current?.chat_id !== chatId) {
      selectedCanonicalChatRef.current = null;
      setSelectedCanonicalChat(null);
    }
  }, []);

  const restoreProjectTabState = useCallback(
    (id: string, state: CachedProjectTabState, requestedView?: AppView) => {
      const nextGraph = state.project.graph;
      const presented = applyHumanDraft(nextGraph, state.humanDraft);
      cacheProjectTabState(projectTabStatesRef.current, id, state);
      renderedRevisionRef.current = nextGraph.revision;
      authoritativeProjectId.current = id;
      setProject(state.project);
      setGraph(nextGraph);
      setPaper(state.project.paper);
      setProjectHeaderCollapsed(state.projectHeaderCollapsed);
      setRunScope([...state.runScope]);
      setSelectedNode(
        state.selectedNodeId ? (presented.nodes[state.selectedNodeId] ?? null) : null,
      );
      setCompanionNode(
        state.companionNodeId ? (presented.nodes[state.companionNodeId] ?? null) : null,
      );
      setDetailFocusTokens({ ...state.detailFocusTokens });
      setSelectedExperimentRunId(state.selectedExperimentRunId);
      setFocusExperimentRunId(state.focusExperimentRunId);
      setExperimentStopId(null);
      setDockedNodeIds(state.dockedNodeIds.filter((nodeId) => Boolean(nextGraph.nodes[nodeId])));
      setFloatingChat(state.floatingChat ? { ...state.floatingChat } : null);
      setDraftConversations([...state.draftConversations]);
      selectChat(state.selectedChatId);
      setUnreadChatTaskIds(new Set(state.unreadChatTaskIds));
      chatSummaryRefreshGeneration.current += 1;
      chatSummariesRef.current = [...state.chatSummaries];
      setChatSummaries([...state.chatSummaries]);
      setChatSummaryTotal(state.chatSummaryTotal);
      setChatSummaryNextOffset(state.chatSummaryNextOffset);
      setChatSummariesLoading(false);
      setChatTranscripts(new Map(state.chatTranscripts));
      selectedCanonicalChatRef.current = state.selectedCanonicalChat;
      setSelectedCanonicalChat(state.selectedCanonicalChat);
      chatTaskStatuses.current = new Map(state.chatTaskStatuses);
      setDagRelationFocusId(state.dagRelationFocusId);
      setRetryTask(state.retryTask);
      setHumanDraft(state.humanDraft);
      setSyncingDraft(false);
      setTasks([...state.tasks]);
      setLatestRevisionSummary(state.latestRevisionSummary);
      setHistoryRevisionSummaries([...state.historyRevisionSummaries]);
      setHistorySummariesRevision(state.historySummariesRevision);
      setHistorySummariesError(state.historySummariesError);
      setProjectHistoryOpen(state.projectHistoryOpen);
      setUsage(state.usage);
      setWatchers([...state.watchers]);
      setTaskInspectorId(state.taskInspectorId);
      setInspectedTask(state.inspectedTask);
      setActivityTaskId(state.activityTaskId);
      setDismissedTaskIds(new Set(state.dismissedTaskIds));
      setDismissedHistoryNoticeIds(new Set(state.dismissedHistoryNoticeIds));
      panelScrollRef.current = new Map(state.viewState.panelScroll);
      researchSubviewRef.current = state.viewState.researchSubview;
      const viewportRef = projectViewportRef(dagViewportRefsRef.current, id);
      viewportRef.current = state.viewState.dagViewport ? { ...state.viewState.dagViewport } : null;
      setView(requestedView ?? state.viewState.view);
      setProjectReconciliation("authoritative");
      setLoading(false);
    },
    [selectChat],
  );

  const applyProjectSnapshot = useCallback(
    (nextProject: ProjectSnapshot, preserveReadiness: boolean) => {
      const nextGraph = nextProject.graph;
      if (
        !cachedSnapshotCanReplace(activeProjectId.current, renderedRevisionRef.current, nextProject)
      )
        return;
      renderedRevisionRef.current = nextGraph.revision;
      setProject((current) =>
        preserveReadiness ? preserveProjectReadiness(nextProject, current) : nextProject,
      );
      setGraph(nextGraph);
      setPaper(nextProject.paper);
      setHumanDraft((current) => {
        if (!current) return null;
        const rebased = normalizeHumanDraft(current, nextGraph);
        const retained = humanDraftChangeCount(rebased) > 0 ? rebased : null;
        try {
          persistProjectHumanDraft(localStorage, nextProject.id, retained);
        } catch {
          // The in-memory draft remains usable if browser storage is unavailable.
        }
        return retained;
      });
      setSelectedNode((current) => (current ? (nextGraph.nodes[current.id] ?? current) : null));
      setCompanionNode((current) => (current ? (nextGraph.nodes[current.id] ?? current) : null));
      setDockedNodeIds((current) => current.filter((nodeId) => nextGraph.nodes[nodeId]));
      setRunScope((current) =>
        current.length
          ? current.filter((item) => nextProject.project_truth_scope.includes(item))
          : nextProject.default_run_truth_scope,
      );
    },
    [],
  );

  const refreshChatSummaries = useCallback(async (requestedProjectId: string, base: string) => {
    const generation = ++chatSummaryRefreshGeneration.current;
    setChatSummariesLoading(true);
    try {
      const page = await loadChatSummaryPage(base, 0, api);
      if (
        activeProjectId.current !== requestedProjectId ||
        generation !== chatSummaryRefreshGeneration.current
      )
        return;
      const selectedChatId = selectedChatIdRef.current;
      const previousSummary = selectedChatId
        ? (chatSummariesRef.current.find((summary) => summary.chat_id === selectedChatId) ??
          (selectedCanonicalChatRef.current?.chat_id === selectedChatId
            ? selectedCanonicalChatRef.current
            : null))
        : null;
      let validation: ChatTranscript | null | undefined;
      if (
        selectedChatId &&
        previousSummary &&
        !page.items.some((summary) => summary.chat_id === selectedChatId)
      ) {
        try {
          validation = await loadChatTranscript(base, selectedChatId, api);
        } catch (error) {
          if (error instanceof ApiError && error.status === 404) validation = null;
          else throw error;
        }
      }
      if (
        activeProjectId.current !== requestedProjectId ||
        generation !== chatSummaryRefreshGeneration.current
      )
        return;
      const nextSummaries = mergeChatSummaryPage([], page.items, "refresh");
      chatSummariesRef.current = nextSummaries;
      setChatSummaries(nextSummaries);
      setChatSummaryTotal(page.total);
      setChatSummaryNextOffset(nextChatSummaryOffset(page));
      if (selectedChatIdRef.current === selectedChatId) {
        const reconciliation = reconcileChatSelectionAfterRefresh(
          selectedChatId,
          previousSummary,
          nextSummaries,
          validation,
        );
        selectedChatIdRef.current = reconciliation.selectedChatId;
        setSelectedChatId(reconciliation.selectedChatId);
        selectedCanonicalChatRef.current = reconciliation.retainedSummary;
        setSelectedCanonicalChat(reconciliation.retainedSummary);
        if (validation) {
          setChatTranscripts((current) => new Map(current).set(validation.chat_id, validation));
        } else if (reconciliation.deleteTranscript && selectedChatId) {
          setDraftConversations((current) =>
            current.filter((draft) => draft.chatId !== selectedChatId),
          );
          setChatTranscripts((current) => {
            if (!current.has(selectedChatId)) return current;
            const next = new Map(current);
            next.delete(selectedChatId);
            return next;
          });
        }
      }
    } finally {
      if (
        activeProjectId.current === requestedProjectId &&
        generation === chatSummaryRefreshGeneration.current
      ) {
        setChatSummariesLoading(false);
      }
    }
  }, []);

  const loadMoreChatSummaries = useCallback(async () => {
    if (!projectId || !apiBase || chatSummariesLoading || chatSummaryNextOffset >= chatSummaryTotal)
      return;
    const requestedProjectId = projectId;
    const generation = chatSummaryRefreshGeneration.current;
    const offset = chatSummaryNextOffset;
    setChatSummariesLoading(true);
    try {
      const page = await loadChatSummaryPage(apiBase, offset, api);
      if (
        activeProjectId.current !== requestedProjectId ||
        generation !== chatSummaryRefreshGeneration.current
      )
        return;
      const nextSummaries = mergeChatSummaryPage(chatSummariesRef.current, page.items, "append");
      chatSummariesRef.current = nextSummaries;
      setChatSummaries(nextSummaries);
      setChatSummaryTotal(page.total);
      setChatSummaryNextOffset(nextChatSummaryOffset(page));
    } catch (error) {
      if (
        activeProjectId.current === requestedProjectId &&
        generation === chatSummaryRefreshGeneration.current
      ) {
        setNotice({
          kind: "error",
          text: `Chats could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
        });
      }
    } finally {
      if (
        activeProjectId.current === requestedProjectId &&
        generation === chatSummaryRefreshGeneration.current
      ) {
        setChatSummariesLoading(false);
      }
    }
  }, [apiBase, chatSummariesLoading, chatSummaryNextOffset, chatSummaryTotal, projectId]);

  const reload = useCallback(
    async (includeTasks = true) => {
      if (!projectId) return;
      const requestedProjectId = projectId;
      const base = `/api/projects/${encodeURIComponent(requestedProjectId)}`;
      const projectRequest = api<ProjectSnapshot>(base).then((nextProject) => {
        if (activeProjectId.current !== requestedProjectId) return;
        applyProjectSnapshot(nextProject, authoritativeProjectId.current === requestedProjectId);
        authoritativeProjectId.current = requestedProjectId;
        setProjectReconciliation("authoritative");
      });
      const tasksRequest = includeTasks
        ? api<AgentTask[]>(`${base}/tasks`).then((nextTasks) => {
            if (activeProjectId.current === requestedProjectId) setTasks(nextTasks);
          })
        : Promise.resolve();
      const usageRequest = api<AgentUsageSnapshot>(`${base}/usage`)
        .then((nextUsage) => {
          if (activeProjectId.current === requestedProjectId) setUsage(nextUsage);
        })
        .catch((error) => {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
          if (activeProjectId.current === requestedProjectId) setUsage(null);
        });
      const watchersRequest = api<WatcherRecord[]>(`${base}/watchers`).then((nextWatchers) => {
        if (activeProjectId.current === requestedProjectId) setWatchers(nextWatchers);
      });
      const chatsRequest = refreshChatSummaries(requestedProjectId, base).catch((error) => {
        if (activeProjectId.current === requestedProjectId) {
          setNotice({
            kind: "error",
            text: `Chats could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
          });
        }
      });
      await Promise.all([
        projectRequest,
        tasksRequest,
        usageRequest,
        watchersRequest,
        chatsRequest,
      ]);
    },
    [applyProjectSnapshot, projectId, refreshChatSummaries],
  );
  reloadRef.current = reload;

  const reloadAuthoritativeProject = useCallback((requestedProjectId?: string | null) => {
    const activeId = requestedProjectId ?? activeProjectId.current;
    if (!activeId || activeProjectId.current !== activeId) return Promise.resolve();
    if (authoritativeReloadInFlight.current?.projectId === activeId) {
      return authoritativeReloadInFlight.current.request;
    }
    const request = reloadRef.current().finally(() => {
      if (authoritativeReloadInFlight.current?.request === request) {
        authoritativeReloadInFlight.current = null;
      }
    });
    authoritativeReloadInFlight.current = { projectId: activeId, request };
    return request;
  }, []);

  const heartbeatProjectCache = useCallback(
    (requestedProjectId: string): Promise<void> =>
      singleFlightProjectCacheHeartbeat(
        projectCacheHeartbeatInFlight.current,
        requestedProjectId,
        async () => {
          const base = `/api/projects/${encodeURIComponent(requestedProjectId)}`;
          const observedRevision = await loadCanonicalRevision(api, base);
          const tabIsOpen = () =>
            openProjectTabsRef.current.some((tab) => tab.id === requestedProjectId);
          if (!tabIsOpen()) return;
          if (activeProjectId.current === requestedProjectId) {
            if (canonicalRevisionNeedsReload(observedRevision, renderedRevisionRef.current)) {
              await reloadAuthoritativeProject(requestedProjectId);
            }
            return;
          }

          const retained = inactiveProjectTabState(
            projectTabStatesRef.current,
            openProjectTabsRef.current,
            activeProjectId.current,
            requestedProjectId,
          );
          if (!retained || observedRevision <= retained.project.graph.revision) return;
          const snapshot = await api<ProjectSnapshot>(`${base}/cached`);
          const current = inactiveProjectTabState(
            projectTabStatesRef.current,
            openProjectTabsRef.current,
            activeProjectId.current,
            requestedProjectId,
          );
          if (!current) {
            if (!tabIsOpen()) return;
            if (
              activeProjectId.current === requestedProjectId &&
              canonicalRevisionNeedsReload(snapshot.graph.revision, renderedRevisionRef.current)
            ) {
              await reloadAuthoritativeProject(requestedProjectId);
            }
            return;
          }
          const next = reconcileInactiveProjectTabState(current, snapshot);
          if (next === current) return;
          cacheProjectTabState(projectTabStatesRef.current, requestedProjectId, next);
          try {
            persistProjectHumanDraft(localStorage, requestedProjectId, next.humanDraft);
          } catch {
            // A background cache refresh must not discard the in-memory draft.
          }
        },
      ),
    [reloadAuthoritativeProject],
  );

  useEffect(() => {
    if (!identityReady || identityIssue) return;
    const runHeartbeat = (id: string) => {
      void heartbeatProjectCache(id).catch(() => {
        // Heartbeat failures leave the last usable display cache intact.
      });
    };
    return startProjectCachePolling(
      {
        setInterval: (callback, delay) => window.setInterval(callback, delay),
        clearInterval: (intervalId) => window.clearInterval(intervalId),
      },
      {
        isHidden: pageIsHidden,
        listen: (callback) => {
          document.addEventListener("visibilitychange", callback);
          return () => document.removeEventListener("visibilitychange", callback);
        },
      },
      () => projectIdsForCacheHeartbeat(openProjectTabsRef.current).forEach(runHeartbeat),
      () => {
        const activeId = activeProjectId.current;
        if (activeId) runHeartbeat(activeId);
      },
    );
  }, [heartbeatProjectCache, identityIssue, identityReady]);

  useEffect(() => {
    if (!projectId || !apiBase || project?.id !== projectId) return;
    const requestedProjectId = projectId;
    const requestedRevision = graph.revision;
    if (requestedRevision === 0) {
      setLatestRevisionSummary(null);
      return;
    }
    let cancelled = false;
    setLatestRevisionSummary((current) =>
      current?.to_revision === requestedRevision ? current : null,
    );
    void api<RevisionSummary[]>(revisionSummariesUrl(apiBase, requestedRevision))
      .then((summaries) => {
        if (cancelled || activeProjectId.current !== requestedProjectId) return;
        setLatestRevisionSummary(
          summaries.find((summary) => summary.to_revision === requestedRevision) ?? null,
        );
      })
      .catch((error) => {
        if (cancelled || activeProjectId.current !== requestedProjectId) return;
        setLatestRevisionSummary(null);
        setNotice({
          kind: "error",
          text: `Latest project change could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, graph.revision, project?.id, projectId]);

  useEffect(() => {
    if (!projectHistoryOpen || !projectId || !apiBase || project?.id !== projectId) return;
    const requestedProjectId = projectId;
    const requestedRevision = graph.revision;
    let cancelled = false;
    setHistorySummariesError(null);
    void api<RevisionSummary[]>(revisionSummariesUrl(apiBase))
      .then((summaries) => {
        if (cancelled || activeProjectId.current !== requestedProjectId) return;
        setHistoryRevisionSummaries(summaries);
        setHistorySummariesRevision(requestedRevision);
      })
      .catch((error) => {
        if (cancelled || activeProjectId.current !== requestedProjectId) return;
        const message = `Project history could not be loaded: ${error instanceof Error ? error.message : String(error)}`;
        setHistoryRevisionSummaries([]);
        setHistorySummariesError(message);
        setHistorySummariesRevision(requestedRevision);
        setNotice({ kind: "error", text: message });
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, graph.revision, project?.id, projectHistoryOpen, projectId]);

  const refreshDesktopUpdate = useCallback(async () => {
    if (!desktop) return;
    try {
      const result = await checkDesktopUpdate();
      setDesktopUpdate(result?.available ? result : null);
      setUpdateError(result?.enabled === false && result.reason ? result.reason : null);
    } catch (error) {
      setUpdateError(error instanceof Error ? error.message : String(error));
    }
  }, [desktop]);

  const requestActorName = useCallback((): Promise<boolean> => {
    if (actorNamePromptResolver.current) return Promise.resolve(false);
    setActorNameDraft(actorIdentityRef.current?.user.display_name ?? "");
    setActorNameError(null);
    setActorNamePromptOpen(true);
    return new Promise((resolve) => {
      actorNamePromptResolver.current = resolve;
    });
  }, []);

  const settleActorNamePrompt = useCallback((saved: boolean) => {
    const resolve = actorNamePromptResolver.current;
    actorNamePromptResolver.current = null;
    setActorNamePromptOpen(false);
    setActorNameSaving(false);
    setActorNameError(null);
    resolve?.(saved);
  }, []);

  const saveActorName = useCallback(async () => {
    const displayName = actorNameDraft.trim();
    if (!displayName || actorNameSaving) return;
    setActorNameSaving(true);
    setActorNameError(null);
    try {
      const saved = await api<IdentityResponse>("/api/identity", {
        method: "PATCH",
        body: JSON.stringify({ display_name: displayName }),
      });
      setActorIdentity(saved);
      setActorIdentityError(null);
      settleActorNamePrompt(true);
    } catch (error) {
      setActorNameError(error instanceof Error ? error.message : String(error));
      setActorNameSaving(false);
    }
  }, [actorNameDraft, actorNameSaving, settleActorNamePrompt]);

  useEffect(() => {
    const onIdentity = (event: Event) => {
      const detail = (event as CustomEvent<BackendIdentityEventDetail>).detail;
      setIdentityReady(true);
      setIdentityIssue(detail.ok ? null : detail.message || "RCP could not verify its backend.");
      if (detail.health) {
        verifiedHealthRef.current = detail.health;
        setVerifiedHealth(detail.health);
        if (detail.ok) pinApiInstance(detail.health.instance_id);
      }
    };
    window.addEventListener(BACKEND_IDENTITY_EVENT, onIdentity);
    registerMutationFailureHandler(verifyIdentityAfterMutationFailure);
    void establishBackendIdentity();
    return () => {
      registerMutationFailureHandler(null);
      window.removeEventListener(BACKEND_IDENTITY_EVENT, onIdentity);
    };
  }, []);

  useEffect(() => {
    registerIdentityNameRequiredHandler(requestActorName);
    return () => {
      registerIdentityNameRequiredHandler(null);
      const resolve = actorNamePromptResolver.current;
      actorNamePromptResolver.current = null;
      resolve?.(false);
    };
  }, [requestActorName]);

  useEffect(() => {
    if (!identityReady || identityIssue) return;
    let stopped = false;
    setActorIdentityError(null);
    void api<IdentityResponse>("/api/identity")
      .then((identity) => {
        if (!stopped) setActorIdentity(identity);
      })
      .catch((error) => {
        if (!stopped) setActorIdentityError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      stopped = true;
    };
  }, [identityIssue, identityReady]);

  useEffect(() => {
    if (!desktop) return;
    let stopped = false;
    const cleanups: Array<() => void> = [];
    const prepareShow = async () => {
      try {
        const identity = await reverifyBackendIdentity("prepare-show");
        if (identity.ok) {
          if (activeProjectId.current) {
            const visibleProjectId = activeProjectId.current;
            const nextTasks = await api<AgentTask[]>(
              `/api/projects/${encodeURIComponent(visibleProjectId)}/tasks`,
            );
            if (activeProjectId.current === visibleProjectId) setTasks(nextTasks);
            setProjectReconciliation("reconciling");
            void reloadRef.current(false).catch((error) => {
              if (activeProjectId.current !== visibleProjectId || stopped) return;
              setProjectReconciliation("failed");
              setNotice({
                kind: "error",
                text: error instanceof Error ? error.message : String(error),
              });
            });
          } else {
            const nextProjects = await api<ProjectCard[]>("/api/projects");
            if (!stopped) setProjects(nextProjects);
          }
          await refreshDesktopUpdate();
        }
      } catch (error) {
        if (!stopped)
          setNotice({
            kind: "error",
            text: error instanceof Error ? error.message : String(error),
          });
      } finally {
        try {
          await desktopShowReady();
        } catch (error) {
          if (!stopped)
            setNotice({
              kind: "error",
              text: error instanceof Error ? error.message : String(error),
            });
        }
      }
    };
    void Promise.all([
      listenDesktopEvent("rcp://prepare-show", prepareShow),
      listenDesktopEvent<{ message?: string }>("rcp://backend-mismatch", async (payload) => {
        if (!stopped && payload.message) setIdentityIssue(payload.message);
        await reverifyBackendIdentity("desktop-backend-mismatch");
      }),
      listenDesktopEvent<{ version?: string }>("rcp://update-ready", (payload) => {
        if (stopped) return;
        setDesktopUpdate((current) => ({
          enabled: true,
          available: true,
          version: payload.version ?? current?.version,
          current_version: current?.current_version,
          active_agent_tasks:
            current?.active_agent_tasks ?? verifiedHealthRef.current?.active_agent_tasks ?? 0,
        }));
      }),
    ]).then((nextCleanups) => {
      if (stopped) nextCleanups.forEach((cleanup) => cleanup());
      else {
        cleanups.push(...nextCleanups);
        if (!initialShowHandshake.current) {
          initialShowHandshake.current = true;
          void prepareShow();
        }
      }
    });
    void refreshDesktopUpdate();
    return () => {
      stopped = true;
      cleanups.forEach((cleanup) => cleanup());
    };
  }, [desktop, refreshDesktopUpdate]);

  const refreshReadiness = useCallback(async () => {
    if (!apiBase) return;
    try {
      const readiness = await loadProjectReadiness(apiBase, true);
      setProject((current) => (current ? { ...current, ...readiness } : current));
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
  }, [apiBase]);

  const ensureProjectReadiness = useCallback(() => {
    if (
      !apiBase ||
      !projectId ||
      project?.id !== projectId ||
      projectReconciliation !== "authoritative" ||
      readinessRequestedProjectIds.current.has(projectId)
    )
      return;
    const requestedProjectId = projectId;
    readinessRequestedProjectIds.current.add(requestedProjectId);
    const readCachedReadiness = (completedFollowUps: number) => {
      void loadProjectReadiness(apiBase)
        .then((readiness) => {
          if (activeProjectId.current !== requestedProjectId) return;
          setProject((current) =>
            current?.id === requestedProjectId ? { ...current, ...readiness } : current,
          );
          if (
            shouldPollProviderSkillReadiness(
              readiness.provider_skill_inventories,
              completedFollowUps,
            )
          ) {
            const timeoutId = window.setTimeout(() => {
              providerSkillReadinessPoll.current = null;
              if (activeProjectId.current !== requestedProjectId) return;
              readCachedReadiness(completedFollowUps + 1);
            }, PROVIDER_SKILL_READINESS_POLL_DELAY_MS);
            providerSkillReadinessPoll.current = { projectId: requestedProjectId, timeoutId };
          } else {
            providerSkillReadinessPoll.current = null;
          }
        })
        .catch((error) => {
          readinessRequestedProjectIds.current.delete(requestedProjectId);
          if (providerSkillReadinessPoll.current?.projectId === requestedProjectId) {
            providerSkillReadinessPoll.current = null;
          }
          if (activeProjectId.current !== requestedProjectId) return;
          setNotice({
            kind: "error",
            text: error instanceof Error ? error.message : String(error),
          });
        });
    };
    readCachedReadiness(0);
  }, [apiBase, project?.id, projectId, projectReconciliation]);

  useEffect(() => {
    return () => {
      const pending = providerSkillReadinessPoll.current;
      if (pending?.projectId === projectId) {
        window.clearTimeout(pending.timeoutId);
        providerSkillReadinessPoll.current = null;
      }
    };
  }, [projectId]);

  const refreshUsage = useCallback(async () => {
    if (!apiBase) return;
    try {
      const nextUsage = await api<AgentUsageSnapshot>(`${apiBase}/usage`);
      if (activeProjectId.current === projectId) setUsage(nextUsage);
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
  }, [apiBase, projectId]);

  const updatePaper = useCallback((nextPaper: PaperSnapshot) => {
    setPaper(nextPaper);
    setProject((current) => (current ? { ...current, paper: nextPaper } : current));
  }, []);

  useEffect(() => {
    const handleHashChange = () => {
      const route = parseProjectHash(window.location.hash);
      if (route.projectId !== activeProjectId.current) {
        rememberProjectState(activeProjectId.current);
      }
      setSetupOpen(isSetupRoute());
      setProjectId(route.projectId);
      setView(route.view);
      setSelectedExperimentRunId(route.experimentId);
      setFocusExperimentRunId(route.experimentId);
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, [rememberProjectState]);

  useLayoutEffect(() => {
    if (!identityReady || identityIssue) return;
    const requestedRoute = parseProjectHash(window.location.hash);
    const routeMatchesProject = requestedRoute.projectId === projectId;
    const retainedOpen = projectId
      ? projectTabStateForOpen(projectTabStatesRef.current, projectId)
      : null;
    const retained = retainedOpen?.state;
    setNotice(null);
    if (projectId && retained) {
      restoreProjectTabState(
        projectId,
        retained,
        routeMatchesProject && requestedRoute.experimentId ? requestedRoute.view : undefined,
      );
    } else {
      setLoading(true);
      setProjectReconciliation("opening");
      authoritativeProjectId.current = null;
      renderedRevisionRef.current = 0;
      setProject(null);
      setGraph(emptyGraph);
      setPaper(null);
      setSelectedNode(null);
      setCompanionNode(null);
      setSelectedExperimentRunId(routeMatchesProject ? requestedRoute.experimentId : null);
      setFocusExperimentRunId(routeMatchesProject ? requestedRoute.experimentId : null);
      setExperimentStopId(null);
      setDockedNodeIds([]);
      setFloatingChat(null);
      setDraftConversations([]);
      selectChat(null);
      setUnreadChatTaskIds(new Set());
      chatSummaryRefreshGeneration.current += 1;
      chatSummariesRef.current = [];
      setChatSummaries([]);
      setChatSummaryTotal(0);
      setChatSummaryNextOffset(0);
      setChatSummariesLoading(false);
      setChatTranscripts(new Map());
      chatTaskStatuses.current = new Map();
      setDagRelationFocusId(null);
      setRetryTask(null);
      setRunScope([]);
      setTasks([]);
      setLatestRevisionSummary(null);
      setHistoryRevisionSummaries([]);
      setHistorySummariesRevision(null);
      setHistorySummariesError(null);
      setProjectHistoryOpen(false);
      setUsage(null);
      setWatchers([]);
      setTaskInspectorId(null);
      setInspectedTask(null);
      setActivityTaskId(null);
      setDismissedTaskIds(readDismissedTaskIds(projectId));
      setDismissedHistoryNoticeIds(readDismissedHistoryNoticeIds(projectId));
      setProjectHeaderCollapsed(readProjectHeaderCollapsed(projectId));
      setHumanDraft(null);
      setSyncingDraft(false);
      panelScrollRef.current = new Map();
      researchSubviewRef.current = "scientific";
      setView(routeMatchesProject ? requestedRoute.view : "overview");
    }
    if (setupOpen) {
      setLoading(false);
      return;
    }
    if (!projectId) {
      api<ProjectCard[]>("/api/projects")
        .then(setProjects)
        .catch((error) =>
          setNotice({
            kind: "error",
            text: error instanceof Error ? error.message : String(error),
          }),
        )
        .finally(() => setLoading(false));
      return;
    }
    if (!retained) {
      try {
        setHumanDraft(deserializeHumanDraft(localStorage.getItem(humanDraftStorageKey(projectId))));
      } catch (error) {
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      }
    }
    let cancelled = false;
    const openProject = async () => {
      const cachedPath = `/api/projects/${encodeURIComponent(projectId)}/cached`;
      try {
        const cachedProject = await api<ProjectSnapshot>(cachedPath);
        if (cancelled || activeProjectId.current !== projectId) return;
        applyProjectSnapshot(cachedProject, false);
        setProjectReconciliation("authoritative");
        setLoading(false);
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404) && !cancelled) {
          setNotice({
            kind: "error",
            text: error instanceof Error ? error.message : String(error),
          });
        }
      }
      try {
        await reload();
      } catch (error) {
        if (cancelled || activeProjectId.current !== projectId) return;
        if (!retained && authoritativeProjectId.current !== projectId) {
          setProjectReconciliation("failed");
        }
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      } finally {
        if (!cancelled && activeProjectId.current === projectId) setLoading(false);
      }
    };
    void openProject();
    return () => {
      cancelled = true;
    };
  }, [
    applyProjectSnapshot,
    identityIssue,
    identityReady,
    projectId,
    reload,
    restoreProjectTabState,
    selectChat,
    setupOpen,
  ]);

  useEffect(() => {
    if (!identityReady || identityIssue || projectId || setupOpen) return;
    let stopped = false;
    let timer = 0;
    const schedule = () => {
      timer = window.setTimeout(() => void poll(), EXPERIMENT_BOARD_POLL_DELAY_MS);
    };
    const poll = async () => {
      if (stopped) return;
      if (pageIsHidden()) {
        schedule();
        return;
      }
      try {
        const nextEntries = await api<ExperimentLoopIndexEntry[]>("/api/experiment-loops");
        if (!stopped) setExperimentLoops(nextEntries);
      } catch (error) {
        if (!stopped) {
          setNotice({
            kind: "error",
            text: `Experiment board could not be refreshed: ${error instanceof Error ? error.message : String(error)}`,
          });
        }
      }
      if (!stopped) schedule();
    };
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [identityIssue, identityReady, projectId, setupOpen]);

  useEffect(() => {
    if (!project || project.id !== projectId) return;
    const nextTabs = openProjectTab(openProjectTabsRef.current, {
      id: project.id,
      name: project.name,
    });
    if (nextTabs === openProjectTabsRef.current) return;
    openProjectTabsRef.current = nextTabs;
    setOpenProjectTabs(nextTabs);
  }, [project, projectId]);

  useEffect(() => {
    if (projectReconciliation === "authoritative") ensureProjectReadiness();
  }, [ensureProjectReadiness, projectReconciliation]);

  useEffect(() => {
    if (!projectId) return;
    try {
      localStorage.setItem(
        projectHeaderCollapsedStorageKey(projectId),
        String(projectHeaderCollapsed),
      );
    } catch {
      // Layout state is a convenience; storage failures must not affect the project.
    }
  }, [projectHeaderCollapsed, projectId]);

  useEffect(() => {
    try {
      localStorage.setItem("rcp:trust-view", trustView);
    } catch {
      // The chosen view is a convenience; storage failures must not affect the project.
    }
  }, [trustView]);

  useEffect(() => {
    try {
      localStorage.setItem(TEXT_SCALE_STORAGE_KEY, String(textScale));
    } catch {
      // Text size is a convenience; storage failures must not affect the project.
    }
  }, [textScale]);

  useEffect(() => {
    if (!desktop) return;
    void setDesktopWebviewZoom(textScale / 100).catch((error) => {
      setNotice({
        kind: "error",
        text: `Text size could not be applied: ${error instanceof Error ? error.message : String(error)}`,
      });
    });
  }, [desktop, textScale]);

  useEffect(() => {
    if (!desktop) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const action = textScaleShortcut(event);
      if (!action) return;
      event.preventDefault();
      setTextScale((current) => changeTextScale(current, action));
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [desktop]);

  const activeTask = useMemo(() => tasks.find(isActiveTask) ?? null, [tasks]);
  const watchersAwaitingDelivery = useMemo(
    () => watchers.some((watcher) => !watcher.notified),
    [watchers],
  );
  const experimentControlForNode = (node: GraphNode): ExperimentControlState | null => {
    if (!project || node.type !== "experiment") return null;
    const control = project.experiment_control?.[node.id];
    if (!control) return null;
    const operationActive = tasks.some(
      (task) =>
        isActiveTask(task) &&
        task.request.patch_kind === "experiment_loop" &&
        task.request.control_node_id === node.id,
    );
    return operationActive && (!control.active || control.paused)
      ? { ...control, active: true, paused: false }
      : control;
  };
  const retryConfig = useMemo(
    () => (retryTask && project ? taskRetryConfig(retryTask, project) : null),
    [project, retryTask],
  );
  const mutationsDisabled = graphMutationsDisabled(graph);
  const presentedGraph = useMemo(
    () => (mutationsDisabled ? graph : applyHumanDraft(graph, humanDraft)),
    [graph, humanDraft, mutationsDisabled],
  );
  const glossaryIndex = useMemo(
    () => buildGlossaryIndex(presentedGraph.glossary),
    [presentedGraph.glossary, presentedGraph.revision],
  );
  const openNode = (node: GraphNode | null) => {
    if (!node) return;
    setDockedNodeIds((current) => current.filter((nodeId) => nodeId !== node.id));
    if (selectedNode?.id === node.id) {
      setDetailFocusTokens((current) => ({ ...current, original: current.original + 1 }));
      return;
    }
    if (companionNode?.id === node.id) {
      setDetailFocusTokens((current) => ({ ...current, companion: current.companion + 1 }));
      return;
    }
    setSelectedNode(node);
    setCompanionNode(null);
    setDetailFocusTokens((current) => ({ ...current, original: current.original + 1 }));
  };
  const openNodeById = (nodeId: string) => openNode(presentedGraph.nodes[nodeId] ?? null);
  const openRelatedNode = (sourceSlot: DetailWindowSlot, nodeId: string) => {
    const node = presentedGraph.nodes[nodeId];
    if (!node) return;
    setDockedNodeIds((current) => current.filter((id) => id !== nodeId));
    const action = relatedNodeWindowAction(
      sourceSlot,
      nodeId,
      selectedNode?.id ?? null,
      companionNode?.id ?? null,
    );
    if (action.kind === "focus") {
      setDetailFocusTokens((current) => ({
        ...current,
        [action.slot]: current[action.slot] + 1,
      }));
      return;
    }
    const targetSlot = action.slot;
    if (targetSlot === "original") setSelectedNode(node);
    else setCompanionNode(node);
    setDetailFocusTokens((current) => ({
      ...current,
      [targetSlot]: current[targetSlot] + 1,
    }));
  };
  const closeDetailSlot = (slot: DetailWindowSlot) => {
    if (slot === "original") setSelectedNode(null);
    else setCompanionNode(null);
  };
  const dockNode = (nodeId: string, slot: DetailWindowSlot) => {
    setDockedNodeIds((current) => (current.includes(nodeId) ? current : [...current, nodeId]));
    closeDetailSlot(slot);
  };
  const restoreDockedNode = (nodeId: string) => {
    const node = presentedGraph.nodes[nodeId];
    setDockedNodeIds((current) => current.filter((id) => id !== nodeId));
    openNode(node ?? null);
  };
  const dockedNodes = dockedNodeIds.flatMap((nodeId) => {
    const node = presentedGraph.nodes[nodeId];
    return node ? [{ nodeId, node }] : [];
  });
  const nodeTitles = useMemo(
    () =>
      Object.fromEntries(Object.values(presentedGraph.nodes).map((node) => [node.id, node.title])),
    [presentedGraph.nodes],
  );
  const visibleChatSummaries = useMemo(
    () =>
      selectedCanonicalChat &&
      !chatSummaries.some((summary) => summary.chat_id === selectedCanonicalChat.chat_id)
        ? [...chatSummaries, selectedCanonicalChat]
        : chatSummaries,
    [chatSummaries, selectedCanonicalChat],
  );
  const conversations = useMemo(
    () =>
      groupChatConversations(
        visibleChatSummaries,
        tasks,
        nodeTitles,
        project?.name ?? "Project",
        draftConversations,
      ),
    [draftConversations, nodeTitles, project?.name, tasks, visibleChatSummaries],
  );
  const visibleChatIds = useMemo(
    () => [
      ...new Set([
        ...(view === "chats" && selectedChatId ? [selectedChatId] : []),
        ...(floatingChat ? [floatingChat.chatId] : []),
      ]),
    ],
    [floatingChat, selectedChatId, view],
  );
  const visibleChatVersions = visibleChatIds
    .map(
      (chatId) =>
        `${chatId}:${visibleChatSummaries.find((summary) => summary.chat_id === chatId)?.updated_at ?? ""}`,
    )
    .join("|");
  const draftChangeCount = humanDraftChangeCount(humanDraft);
  const committableDraftCount = humanDraftCommittableCount(humanDraft, graph);
  const behindDraftCount = humanDraftBehindCount(humanDraft, graph);
  const ontologyDraftIsStale = humanDraftOntologyIsStale(humanDraft, graph);
  const activityTask = projectActivityTask(tasks, activityTaskId);
  const chatsIndicator = chatIndicator(tasks, unreadChatTaskIds);

  const changeAppTextScale = (action: TextScaleAction) => {
    setTextScale((current) => changeTextScale(current, action));
  };

  const startConversation = (kind: ChatKind, node: GraphNode | null = null): string => {
    const chatId = window.crypto.randomUUID();
    const draft: DraftConversation = {
      chatId,
      kind,
      nodeId: node?.id ?? null,
      title: node?.title ?? project?.name ?? "Project",
    };
    setDraftConversations((current) => [draft, ...current]);
    return chatId;
  };

  const ensureConversation = (kind: ChatKind, node: GraphNode | null = null): string => {
    const existing = latestConversation(conversations, kind, node?.id ?? null);
    return existing?.chatId ?? startConversation(kind, node);
  };

  const openChats = (preferredChatId?: string | null) => {
    const nextChatId =
      preferredChatId ??
      chatEntryConversationId(conversations, activityTask, unreadChatTaskIds, selectedChatId);
    selectChat(nextChatId);
    setFloatingChat(null);
    setSelectedNode(null);
    setCompanionNode(null);
    changeView("chats");
  };

  useLayoutEffect(() => {
    viewRef.current = view;
    if (view === "scientific" || view === "dag") researchSubviewRef.current = view;
    const panel = panelRef.current;
    if (panel) panel.scrollTop = panelScrollRef.current.get(view) ?? 0;
  }, [loading, project?.id, view]);

  useEffect(() => {
    if (mutationsDisabled) setRunDialogOpen(false);
  }, [mutationsDisabled]);

  useEffect(() => {
    if (activityTask && (isActiveTask(activityTask) || activityTask.status === "paused")) {
      setActivityTaskId(activityTask.operation_id);
    }
  }, [activityTask]);

  useEffect(() => {
    const nextStatuses = new Map(chatTaskStatuses.current);
    const visibleChatId = view === "chats" ? selectedChatId : null;
    const newlyTerminal = newlyUnreadChatTaskIds(tasks, chatTaskStatuses.current, visibleChatId);
    const completedChatTasks = newlyUnreadChatTaskIds(tasks, chatTaskStatuses.current, null);
    for (const task of tasks) {
      if (!chatIdForTask(task)) continue;
      nextStatuses.set(task.operation_id, task.status);
    }
    chatTaskStatuses.current = nextStatuses;
    if (newlyTerminal.length) {
      setUnreadChatTaskIds((current) => new Set([...current, ...newlyTerminal]));
    }
    if (completedChatTasks.length) {
      if (projectId) {
        void refreshChatSummaries(projectId, apiBase).catch((error) => {
          setNotice({
            kind: "error",
            text: `Chats could not be refreshed: ${error instanceof Error ? error.message : String(error)}`,
          });
        });
      }
    }
  }, [apiBase, projectId, refreshChatSummaries, selectedChatId, tasks, view]);

  useEffect(() => {
    if (!apiBase || visibleChatIds.length === 0) return;
    let cancelled = false;
    visibleChatIds.forEach((chatId) => {
      if (!visibleChatSummaries.some((summary) => summary.chat_id === chatId)) return;
      void loadChatTranscript(apiBase, chatId, api)
        .then((transcript) => {
          if (cancelled) return;
          setChatTranscripts((current) => new Map(current).set(chatId, transcript));
        })
        .catch((error) => {
          if (!cancelled) {
            setNotice({
              kind: "error",
              text: `Conversation could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
            });
          }
        });
    });
    return () => {
      cancelled = true;
    };
  }, [apiBase, visibleChatVersions]);

  useEffect(() => {
    if (view !== "chats") return;
    if (!selectedChatId) return;
    setUnreadChatTaskIds((current) => {
      const conversation = conversations.find((item) => item.chatId === selectedChatId);
      if (!conversation) return current;
      const next = new Set(current);
      conversation.tasks.forEach((task) => next.delete(task.operation_id));
      return next.size === current.size ? current : next;
    });
  }, [conversations, selectedChatId, view]);

  useEffect(() => {
    if (!projectId || !activeTask) return;
    let stopped = false;
    let timer = 0;
    let consecutiveFailures = 0;
    const schedule = (delay: number) => {
      timer = window.setTimeout(() => void poll(), delay);
    };
    const poll = async () => {
      let next: AgentTask[];
      try {
        next = await api<AgentTask[]>(`/api/projects/${encodeURIComponent(projectId)}/tasks`);
      } catch (error) {
        if (!stopped) {
          setNotice({
            kind: "error",
            text: error instanceof Error ? error.message : String(error),
          });
          void reverifyBackendIdentity("active-task-poll-failure");
          consecutiveFailures += 1;
          schedule(Math.min(8000, 1000 * 2 ** (consecutiveFailures - 1)));
        }
        return;
      }
      if (stopped) return;
      const recoveredAfterFailure = consecutiveFailures > 0;
      consecutiveFailures = 0;
      if (recoveredAfterFailure) void reverifyBackendIdentity("active-task-poll-recovered");
      const current = next.find((task) => task.operation_id === activeTask.operation_id);
      if (current && !isActiveTask(current)) {
        void api<AgentUsageSnapshot>(`/api/projects/${encodeURIComponent(projectId)}/usage`).then(
          (nextUsage) => {
            if (!stopped && activeProjectId.current === projectId) setUsage(nextUsage);
          },
        );
        if (terminalTaskNeedsAuthoritativeProjectReload(current)) {
          try {
            await reloadAuthoritativeProject();
          } catch (error) {
            if (!stopped) {
              setNotice({
                kind: "error",
                text: error instanceof Error ? error.message : String(error),
              });
            }
          }
        } else if (current.kind === "node_chat" || current.kind === "project_chat") {
          try {
            const nextWatchers = await api<WatcherRecord[]>(
              `/api/projects/${encodeURIComponent(projectId)}/watchers`,
            );
            if (!stopped) setWatchers(nextWatchers);
          } catch (error) {
            if (!stopped) {
              setNotice({
                kind: "error",
                text: error instanceof Error ? error.message : String(error),
              });
            }
          }
        }
        if (!stopped) setTasks(next);
        return;
      }
      setTasks(next);
      schedule(1000);
    };
    schedule(500);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [activeTask, projectId, reloadAuthoritativeProject]);

  useEffect(() => {
    if (!projectId || !watchersAwaitingDelivery) return;
    const requestedProjectId = projectId;
    const base = `/api/projects/${encodeURIComponent(requestedProjectId)}`;
    let stopped = false;
    let timer = 0;
    const schedule = () => {
      timer = window.setTimeout(() => void poll(), 5000);
    };
    const poll = async () => {
      try {
        const {
          watchers: nextWatchers,
          tasks: nextTasks,
          project: nextProject,
        } = await loadExperimentWatcherPoll(api, base);
        if (!stopped && activeProjectId.current === requestedProjectId) {
          const unseenWatcherResults = nextTasks.filter(
            (task) =>
              task.request.trigger === "watcher" &&
              !chatTaskStatuses.current.has(task.operation_id) &&
              (task.status === "succeeded" ||
                task.status === "failed" ||
                task.status === "interrupted"),
          );
          applyProjectSnapshot(nextProject, authoritativeProjectId.current === requestedProjectId);
          authoritativeProjectId.current = requestedProjectId;
          setProjectReconciliation("authoritative");
          setWatchers(nextWatchers);
          setTasks(nextTasks);
          if (unseenWatcherResults.length > 0) {
            setUnreadChatTaskIds(
              (current) =>
                new Set([
                  ...current,
                  ...unseenWatcherResults.flatMap((task) => {
                    const chatId = chatIdForTask(task);
                    return chatId && chatId !== selectedChatIdRef.current
                      ? [task.operation_id]
                      : [];
                  }),
                ]),
            );
            void refreshChatSummaries(requestedProjectId, base);
          }
        }
      } catch {
        // The authoritative project reload surfaces persistent API failures.
      } finally {
        if (!stopped) schedule();
      }
    };
    schedule();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [applyProjectSnapshot, projectId, refreshChatSummaries, watchersAwaitingDelivery]);

  const inspectorSummary = tasks.find((task) => task.operation_id === taskInspectorId);
  const inspectorVersion = inspectorSummary?.updated_at;
  useEffect(() => {
    if (!inspectorSummary) return;
    setInspectedTask((current) =>
      current?.operation_id === inspectorSummary.operation_id
        ? { ...current, ...inspectorSummary, events: current.events }
        : current,
    );
  }, [inspectorSummary]);
  useEffect(() => {
    if (!projectId || !taskInspectorId) {
      setInspectedTask(null);
      return;
    }
    let cancelled = false;
    setTaskInspectorLoading(true);
    api<AgentTask>(`/api/projects/${encodeURIComponent(projectId)}/tasks/${taskInspectorId}`)
      .then((task) => {
        if (!cancelled) setInspectedTask(task);
      })
      .catch((error) => {
        if (!cancelled)
          setNotice({
            kind: "error",
            text: error instanceof Error ? error.message : String(error),
          });
      })
      .finally(() => {
        if (!cancelled) setTaskInspectorLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [inspectorVersion, projectId, taskInspectorId]);

  const pendingProposals = useMemo(
    () => Object.values(graph.proposals).filter((item) => item.status === "pending"),
    [graph],
  );
  const attentionDecisions = useMemo(
    () => decisionsAwaitingChoice(Object.values(graph.nodes), presentedGraph.nodes),
    [graph.nodes, presentedGraph.nodes],
  );
  const openBlockers = useMemo(
    () => humanAttentionBlockers(Object.values(graph.nodes), presentedGraph.nodes),
    [graph.nodes, presentedGraph.nodes],
  );
  const attentionBlockerIds = useMemo(
    () => new Set(openBlockers.map((node) => node.id)),
    [openBlockers],
  );
  const rejectedPatches = useMemo(
    () =>
      graph.validation_messages.filter(
        (message) =>
          message.level === "reject" &&
          !dismissedHistoryNoticeIds.has(validationNoticeId(message)) &&
          !(typeof message.patch_revision === "number" && message.patch_revision < graph.revision),
      ),
    [dismissedHistoryNoticeIds, graph.revision, graph.validation_messages],
  );

  const updateHumanDraft = (update: (draft: HumanDraft) => HumanDraft) => {
    if (!projectId || mutationsDisabled) return;
    setNotice(null);
    setHumanDraft((current) => {
      const next = update(current ?? emptyHumanDraft(graph.revision));
      try {
        if (humanDraftChangeCount(next) > 0) {
          localStorage.setItem(humanDraftStorageKey(projectId), serializeHumanDraft(next));
          return next;
        }
        localStorage.removeItem(humanDraftStorageKey(projectId));
        return null;
      } catch (error) {
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
        return next;
      }
    });
  };

  const dismissTaskNotification = (operationId: string) => {
    setDismissedTaskIds((current) => {
      const next = new Set(current);
      next.add(operationId);
      try {
        localStorage.setItem(
          taskNotificationStorageKey(projectId),
          serializeDismissedTaskIds(next),
        );
      } catch {}
      return next;
    });
    if (taskInspectorId === operationId) {
      setTaskInspectorId(null);
      setInspectedTask(null);
    }
  };

  const dismissHistoryNotices = (messages: ValidationMessage[]) => {
    const ids = messages.map(validationNoticeId);
    setDismissedHistoryNoticeIds((current) => {
      const next = new Set(current);
      ids.forEach((id) => next.add(id));
      try {
        localStorage.setItem(historyNoticeStorageKey(projectId), JSON.stringify([...next].sort()));
      } catch {}
      return next;
    });
  };

  const resetHumanDraft = () => {
    if (!projectId) return;
    try {
      localStorage.removeItem(humanDraftStorageKey(projectId));
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
    setHumanDraft(null);
  };

  const syncHumanDraft = async () => {
    if (!projectId || !humanDraft || syncingDraft || ontologyDraftIsStale || mutationsDisabled)
      return;
    const normalized = normalizeHumanDraft(humanDraft, graph);
    if (humanDraftCommittableCount(normalized, graph) === 0) return;
    setSyncingDraft(true);
    setNotice(null);
    try {
      const nextGraph = await api<GraphState>(`${apiBase}/sync`, {
        method: "POST",
        body: JSON.stringify(toHumanSyncRequest(normalized, graph)),
      });
      const retained = retainBehindDraftAfterSync(normalized, graph, nextGraph);
      setHumanDraft(retained);
      try {
        persistProjectHumanDraft(localStorage, projectId, retained);
      } catch (error) {
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      }
      setGraph(nextGraph);
      setProject((current) => (current ? projectWithGraph(current, nextGraph) : current));
      setSelectedNode((current) => (current ? (nextGraph.nodes[current.id] ?? null) : null));
      setCompanionNode((current) => (current ? (nextGraph.nodes[current.id] ?? null) : null));
      await reload();
      setNotice({ kind: "info", text: `Synced revision ${nextGraph.revision}.` });
    } catch (error) {
      const failure = humanSyncFailure(error);
      setNotice({ kind: "error", text: failure.text });
      if (failure.revisionConflict) {
        try {
          await reload();
        } catch {}
      }
    } finally {
      setSyncingDraft(false);
    }
  };

  const recordStartedTask = (task: AgentTask) => {
    setTasks((current) => [
      task,
      ...current.filter((item) => item.operation_id !== task.operation_id),
    ]);
    setActivityTaskId(task.operation_id);
    setDismissedTaskIds((current) => {
      const next = new Set(current);
      next.delete(task.operation_id);
      return next;
    });
    setNotice(null);
  };

  const startAgentTask = async (
    kind: AgentTaskKind,
    request: AgentTaskRequest,
  ): Promise<AgentTask> => {
    if (taskStartLock.current || taskStarting)
      throw new Error("Another task start is already being submitted.");
    taskStartLock.current = true;
    setTaskStarting(true);
    try {
      const task = await api<AgentTask>(`${apiBase}/tasks/${kind}`, {
        method: "POST",
        body: JSON.stringify(request),
      });
      recordStartedTask(task);
      return task;
    } finally {
      taskStartLock.current = false;
      setTaskStarting(false);
    }
  };

  const stopWatcher = async (watcherId: string) => {
    if (!apiBase) return;
    try {
      await api<WatcherRecord>(`${apiBase}/watchers/${encodeURIComponent(watcherId)}/stop`, {
        method: "POST",
      });
      setWatchers(await api<WatcherRecord[]>(`${apiBase}/watchers`));
    } catch (error) {
      setNotice({ kind: "error", text: (error as Error).message });
    }
  };

  const stopExperimentLoop = async (nodeId: string) => {
    if (!apiBase || experimentStopId) return;
    setExperimentStopId(nodeId);
    try {
      const control = await api<ExperimentControlState>(
        `${apiBase}/experiments/${encodeURIComponent(nodeId)}/stop`,
        { method: "POST" },
      );
      setProject((current) =>
        current
          ? {
              ...current,
              experiment_control: { ...current.experiment_control, [nodeId]: control },
            }
          : current,
      );
      await reload();
    } catch (error) {
      setNotice({ kind: "error", text: (error as Error).message });
    } finally {
      setExperimentStopId(null);
    }
  };

  const runExperiment = async (node: GraphNode) => {
    if (!project || node.type !== "experiment" || mutationsDisabled) return;
    const control = project.experiment_control?.[node.id];
    if (!control?.ready || control.active) {
      const reason = control?.active
        ? "A control loop is already active for this experiment."
        : (control?.reasons.join(" ") ?? "This experiment is not ready to run.");
      setNotice({ kind: "error", text: reason });
      return;
    }
    if (taskStartLock.current || taskStarting) {
      setNotice({ kind: "error", text: "Another task start is already being submitted." });
      return;
    }
    const chatId = ensureConversation("node_chat", node);
    const profile = project.agent_profiles.node_chat;
    taskStartLock.current = true;
    setTaskStarting(true);
    try {
      const task = await api<AgentTask>(
        `${apiBase}/experiments/${encodeURIComponent(node.id)}/run`,
        {
          method: "POST",
          body: JSON.stringify({
            provider: profile.provider,
            model: profile.model || null,
            reasoning: profile.reasoning,
            run_on: profile.run_on,
            run_truth_scope: runScope.length ? runScope : project.default_run_truth_scope,
            chat_id: chatId,
          }),
        },
      );
      recordStartedTask(task);
      setSelectedExperimentRunId(node.id);
      setFocusExperimentRunId(node.id);
      setSelectedNode(null);
      setCompanionNode(null);
      setFloatingChat(null);
      changeView("execution");
      try {
        await reload();
      } catch (error) {
        setNotice({
          kind: "error",
          text: `The Experiment started, but Runs could not refresh: ${error instanceof Error ? error.message : String(error)}`,
        });
      }
    } catch (caught) {
      setNotice({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      taskStartLock.current = false;
      setTaskStarting(false);
    }
  };

  const runAgent = async (config: AgentRunConfig, scope: string[], message: string | null) => {
    if (!project || taskStarting || mutationsDisabled) return;
    const runKind = project.last_refresh_at ? "refresh" : "seed";
    setRunScope(scope);
    try {
      await startAgentTask(runKind, {
        ...config,
        model: config.model || null,
        run_truth_scope: scope,
        message,
      });
      setRunDialogOpen(false);
    } catch (caught) {
      setNotice({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    }
  };

  const operateTask = async (
    task: AgentTask,
    action: "pause" | "resume" | "retry",
    presentTask = true,
  ) => {
    if (taskActionId) return;
    if (action !== "pause" && mutationsDisabled && taskMayMutateGraph(task)) return;
    setTaskActionId(task.operation_id);
    try {
      const next = await api<AgentTask>(`${apiBase}/tasks/${task.operation_id}/${action}`, {
        method: "POST",
      });
      setTasks((current) => [
        next,
        ...current.filter((item) => item.operation_id !== next.operation_id),
      ]);
      if (presentTask) {
        setActivityTaskId(next.operation_id);
        setTaskInspectorId(next.operation_id);
        setInspectedTask(next);
      }
      setDismissedTaskIds((current) => {
        const updated = new Set(current);
        updated.delete(next.operation_id);
        return updated;
      });
      setNotice(null);
    } catch (caught) {
      const taskError = caught instanceof Error ? caught.message : String(caught);
      if (failedTaskActionNeedsAuthoritativeProjectReload(task, action)) {
        try {
          await reload();
        } catch (reloadError) {
          setNotice({
            kind: "error",
            text: `${taskError} Runs could not refresh: ${reloadError instanceof Error ? reloadError.message : String(reloadError)}`,
          });
          return;
        }
      }
      setNotice({ kind: "error", text: taskError });
    } finally {
      setTaskActionId(null);
    }
  };

  const repairGraphUpdate = async (operationId: string): Promise<void> => {
    if (taskStartLock.current || taskStarting || taskActionId) {
      throw new Error("Another task action is already being submitted.");
    }
    if (mutationsDisabled) {
      throw new Error("Graph repair is unavailable while replay is degraded.");
    }
    taskStartLock.current = true;
    setTaskStarting(true);
    setTaskActionId(operationId);
    try {
      const next = await api<AgentTask>(
        `${apiBase}/tasks/${encodeURIComponent(operationId)}/repair-graph-update`,
        { method: "POST" },
      );
      setTasks((current) => [
        next,
        ...current.filter((item) => item.operation_id !== next.operation_id),
      ]);
      setActivityTaskId(next.operation_id);
      setDismissedTaskIds((current) => {
        const updated = new Set(current);
        updated.delete(next.operation_id);
        return updated;
      });
      setNotice(null);
    } finally {
      taskStartLock.current = false;
      setTaskStarting(false);
      setTaskActionId(null);
    }
  };

  const retryAgentTask = async (task: AgentTask, config: AgentRunConfig) => {
    if (taskActionId || mutationsDisabled) return;
    setTaskActionId(task.operation_id);
    try {
      const next = await api<AgentTask>(`${apiBase}/tasks/${task.operation_id}/retry`, {
        method: "POST",
        body: JSON.stringify(taskRetryRequestBody(task, config)),
      });
      setTasks((current) => [
        next,
        ...current.filter((item) => item.operation_id !== next.operation_id),
      ]);
      if (!isExperimentLoopRecovery(task)) {
        setActivityTaskId(next.operation_id);
        setTaskInspectorId(next.operation_id);
        setInspectedTask(next);
      }
      setRetryTask(null);
      setDismissedTaskIds((current) => {
        const updated = new Set(current);
        updated.delete(next.operation_id);
        return updated;
      });
      setNotice(null);
    } catch (caught) {
      setNotice({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setTaskActionId(null);
    }
  };

  const requestRetry = (task: AgentTask) => {
    if (task.kind === "seed" || task.kind === "refresh") {
      setRetryTask(task);
      return;
    }
    void operateTask(task, "retry");
  };

  const tabForProject = (id: string): ProjectTab => ({
    id,
    name:
      projects.find((item) => item.id === id)?.name ??
      experimentLoops.find((item) => item.project_id === id)?.project_name ??
      (project?.id === id ? project.name : id),
  });

  const setTabs = (nextTabs: ProjectTab[]) => {
    openProjectTabsRef.current = nextTabs;
    setOpenProjectTabs(nextTabs);
  };

  const commitProjectOpen = (id: string, experimentId: string | null = null) => {
    if (projectId !== id) rememberProjectState(projectId);
    setTabs(openProjectTab(openProjectTabsRef.current, tabForProject(id)));
    setSetupOpen(false);
    window.location.hash = experimentId
      ? experimentBoardHref(id, experimentId).slice(1)
      : `/projects/${encodeURIComponent(id)}`;
  };
  const openProject = (id: string, experimentId: string | null = null) => {
    if (desktop) {
      let storedAcknowledgement: string | null = null;
      try {
        storedAcknowledgement = localStorage.getItem(DESKTOP_FOLDER_ACCESS_ACK_KEY);
      } catch {}
      if (needsDesktopFolderAccessAcknowledgement(true, storedAcknowledgement)) {
        setDesktopAccessError(null);
        setPendingDesktopProject({ projectId: id, experimentId });
        return;
      }
    }
    commitProjectOpen(id, experimentId);
  };
  const continueDesktopProjectOpen = () => {
    if (!pendingDesktopProject) return;
    try {
      localStorage.setItem(
        DESKTOP_FOLDER_ACCESS_ACK_KEY,
        desktopFolderAccessAcknowledgementValue(),
      );
      const projectToOpen = pendingDesktopProject;
      setPendingDesktopProject(null);
      setDesktopAccessError(null);
      commitProjectOpen(projectToOpen.projectId, projectToOpen.experimentId);
    } catch (error) {
      setDesktopAccessError(
        `RCP could not record this choice: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  };
  const openSetup = () => {
    setSetupOpen(true);
    setProjectId(null);
    window.location.hash = "/projects/new";
  };
  const returnToProjects = () => {
    rememberProjectState(projectId);
    setSetupOpen(false);
    setProjectId(null);
    window.location.hash = "";
  };

  const deleteProject = async (id: string) => {
    await api(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
    setProjects((current) => current.filter((item) => item.id !== id));
    setExperimentLoops((current) => current.filter((item) => item.project_id !== id));
    projectTabStatesRef.current.delete(id);
    dagViewportRefsRef.current.delete(id);
    setTabs(closeProjectTab(openProjectTabsRef.current, projectId, id).tabs);
    try {
      localStorage.removeItem(humanDraftStorageKey(id));
    } catch {
      // The project is already deleted; a stranded draft key must not fail the action.
    }
  };

  const activateProjectTab = (id: string) => {
    if (id === projectId) return;
    commitProjectOpen(id);
  };

  const closeDockedProject = (id: string) => {
    const result = closeProjectTab(openProjectTabsRef.current, projectId, id);
    if (result.tabs === openProjectTabsRef.current) return;
    projectTabStatesRef.current.delete(id);
    dagViewportRefsRef.current.delete(id);
    setTabs(result.tabs);
    if (id !== projectId) return;
    if (result.activeProjectId) {
      setSetupOpen(false);
      window.location.hash = `/projects/${encodeURIComponent(result.activeProjectId)}`;
    } else {
      setSetupOpen(false);
      setProjectId(null);
      window.location.hash = "";
    }
  };

  useEffect(() => {
    if (!desktop) return;
    const onProjectTabKeyDown = (event: KeyboardEvent) => {
      const action = projectTabShortcut(event, isEditableShortcutTarget(event.target));
      if (!action) return;
      if (action === "index") {
        event.preventDefault();
        returnToProjects();
        return;
      }
      const nextProjectId = adjacentProjectTabId(
        openProjectTabsRef.current,
        projectId,
        action === "previous" ? -1 : 1,
      );
      if (!nextProjectId) return;
      event.preventDefault();
      activateProjectTab(nextProjectId);
    };
    window.addEventListener("keydown", onProjectTabKeyDown);
    return () => window.removeEventListener("keydown", onProjectTabKeyDown);
  });

  const reconnectBackend = async () => {
    if (reconnecting) return;
    setReconnecting(true);
    try {
      if (desktop) {
        const status = await desktopReconnectBackend();
        if (window.location.origin !== new URL(status.base_url).origin) {
          window.location.replace(`${status.base_url}/${window.location.hash}`);
          return;
        }
      }
      await acceptCurrentBackendIdentity();
    } catch (error) {
      setIdentityIssue(error instanceof Error ? error.message : String(error));
    } finally {
      setReconnecting(false);
    }
  };

  const updateHasActiveWork =
    Boolean(activeTask) ||
    (desktopUpdate?.active_agent_tasks ?? verifiedHealth?.active_agent_tasks ?? 0) > 0;
  const applyUpdate = async () => {
    if (!desktopUpdate || updateApplying) return;
    setUpdateApplying(true);
    setUpdateError(null);
    try {
      const identity = await reverifyBackendIdentity("update-apply");
      if (!identity.ok) return;
      if (identity.health) {
        verifiedHealthRef.current = identity.health;
        setVerifiedHealth(identity.health);
      }
      const hasActiveWork = Boolean(activeTask) || (identity.health?.active_agent_tasks ?? 0) > 0;
      if (hasActiveWork && !updateExpanded) {
        setUpdateExpanded(true);
        return;
      }
      await applyDesktopUpdate(hasActiveWork);
    } catch (error) {
      setUpdateError(error instanceof Error ? error.message : String(error));
    } finally {
      setUpdateApplying(false);
    }
  };

  const updateSurface =
    desktop && (desktopUpdate || updateError) ? (
      <DesktopUpdateNotice
        update={desktopUpdate}
        activeWork={updateHasActiveWork}
        expanded={updateExpanded}
        applying={updateApplying}
        error={updateError}
        onExpand={() => setUpdateExpanded(true)}
        onApply={() => void applyUpdate()}
        onDismiss={() => {
          setDesktopUpdate(null);
          setUpdateError(null);
          setUpdateExpanded(false);
        }}
      />
    ) : null;
  const desktopAccessSurface = pendingDesktopProject ? (
    <div className="modal-backdrop desktop-access-backdrop">
      <section
        className="desktop-access-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="desktop-access-title"
        aria-describedby="desktop-access-warning"
      >
        <header>
          <FolderLock size={19} aria-hidden="true" />
          <h2 id="desktop-access-title">Project folder access</h2>
        </header>
        <p id="desktop-access-warning">
          RCP accesses only project folders you choose. macOS may ask for access when a chosen
          project is in Documents, Desktop, or iCloud Drive.
        </p>
        {desktopAccessError && (
          <div className="desktop-access-error" role="alert">
            {desktopAccessError}
          </div>
        )}
        <footer>
          <button
            className="button secondary"
            type="button"
            onClick={() => {
              setPendingDesktopProject(null);
              setDesktopAccessError(null);
            }}
          >
            Not now
          </button>
          <button
            className="button primary"
            type="button"
            autoFocus
            onClick={continueDesktopProjectOpen}
          >
            Continue
          </button>
        </footer>
      </section>
    </div>
  ) : null;
  const actorNameSurface = actorNamePromptOpen ? (
    <div className="modal-backdrop identity-name-backdrop">
      <form
        className="identity-name-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="identity-name-title"
        onSubmit={(event) => {
          event.preventDefault();
          void saveActorName();
        }}
      >
        <header>
          <h2 id="identity-name-title">Choose your name</h2>
        </header>
        <div className="identity-name-body">
          <p>Your chosen name is copied into permanent project history.</p>
          <label>
            Display name
            <input
              autoFocus
              autoComplete="off"
              maxLength={DISPLAY_NAME_MAX_LENGTH}
              value={actorNameDraft}
              onChange={(event) => {
                setActorNameDraft(event.target.value);
                setActorNameError(null);
              }}
            />
          </label>
        </div>
        {actorNameError && (
          <div className="identity-name-error" role="alert">
            {actorNameError}
          </div>
        )}
        <footer>
          <button
            className="button secondary"
            type="button"
            disabled={actorNameSaving}
            onClick={() => settleActorNamePrompt(false)}
          >
            Cancel
          </button>
          <button
            className="button primary"
            type="submit"
            disabled={!actorNameDraft.trim() || actorNameSaving}
          >
            {actorNameSaving ? <LoaderCircle className="spin" size={14} /> : null}
            {actorNameSaving ? "Saving" : "Save and continue"}
          </button>
        </footer>
      </form>
    </div>
  ) : null;
  const acceptanceAgentSurface = (
    <AcceptanceAgentIndicator agentMode={verifiedHealth?.agent_mode} />
  );

  if (!identityReady)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>Verifying the RCP backend</span>
        {acceptanceAgentSurface}
      </div>
    );
  if (identityIssue)
    return (
      <div className="fatal-state reconnect-state">
        <AlertTriangle />
        <h1>Reconnect to RCP</h1>
        <p>{identityIssue}</p>
        <button
          className="button secondary"
          disabled={reconnecting}
          onClick={() => void reconnectBackend()}
        >
          {reconnecting ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}{" "}
          {backendReconnectLabel(desktop)}
        </button>
        {updateSurface}
        {acceptanceAgentSurface}
      </div>
    );
  if (loading)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>{projectId ? "Opening project" : "Reading the project index"}</span>
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {acceptanceAgentSurface}
      </div>
    );
  if (setupOpen)
    return (
      <>
        <ProjectSetup onCancel={returnToProjects} onCreated={openProject} />
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {acceptanceAgentSurface}
      </>
    );
  if (!projectId)
    return (
      <>
        <ProjectLanding
          projects={projects}
          experimentLoops={experimentLoops}
          onOpen={openProject}
          onOpenExperiment={openProject}
          onCreate={openSetup}
          onDelete={deleteProject}
          openProjectTabs={openProjectTabs}
          onActivateProjectTab={activateProjectTab}
          onCloseProjectTab={closeDockedProject}
        />
        {notice && (
          <button className={`toast ${notice.kind}`} onClick={() => setNotice(null)}>
            {notice.text}
          </button>
        )}
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {acceptanceAgentSurface}
      </>
    );
  if (project?.id && project.id !== projectId)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>Opening project</span>
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {acceptanceAgentSurface}
      </div>
    );
  if (!project || !paper)
    return (
      <div className="fatal-state">
        <AlertTriangle />
        <h1>Project could not be opened</h1>
        <p>{notice?.text || "The API returned no project state."}</p>
        <button className="button secondary" onClick={returnToProjects}>
          <ArrowLeft size={15} /> All projects
        </button>
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {acceptanceAgentSurface}
      </div>
    );

  const attentionCount = pendingProposals.length + attentionDecisions.length + openBlockers.length;
  const showTrustFilter = view === "scientific" || view === "dag";
  const runKind = project.last_refresh_at ? "refresh" : "seed";
  const replayWarning = replayFailureLabel(graph);

  return (
    <div className="app-shell overview-shell">
      {acceptanceAgentSurface}
      {!projectHeaderCollapsed && (
        <header className={`project-header${draftChangeCount > 0 ? " has-draft" : ""}`}>
          <div className="project-header-navigation">
            <button className="project-back" onClick={returnToProjects} aria-label="All projects">
              <ArrowLeft size={16} />
            </button>
            <ProjectDock
              tabs={openProjectTabs}
              activeProjectId={projectId}
              onActivate={activateProjectTab}
              onClose={closeDockedProject}
            />
            {projectReconciliation === "reconciling" && (
              <span
                className="project-reconciliation"
                role="status"
                aria-label="Refreshing project state"
              >
                <LoaderCircle className="spin" size={13} aria-hidden="true" />
              </span>
            )}
          </div>
          <div className="project-header-actions" id="project-header-actions">
            <div
              className="project-header-group project-action-group"
              role="group"
              aria-label="Project actions"
            >
              <div className="header-sync-side">
                {draftChangeCount > 0 && (
                  <button
                    className="icon-button draft-reset"
                    aria-label="Reset staged changes"
                    title="Reset staged changes"
                    disabled={projectReconciliation !== "authoritative" || syncingDraft}
                    onClick={resetHumanDraft}
                  >
                    <RotateCcw size={14} />
                  </button>
                )}
                <button
                  className={`button draft-sync${committableDraftCount > 0 ? " active" : ""}${ontologyDraftIsStale ? " stale" : ""}`}
                  disabled={
                    mutationsDisabled ||
                    committableDraftCount === 0 ||
                    syncingDraft ||
                    ontologyDraftIsStale ||
                    !project.canonical_state.reachable
                  }
                  title={ontologyDraftIsStale ? "Ontology draft base is stale" : undefined}
                  aria-label={
                    syncingDraft
                      ? "Syncing staged changes"
                      : ontologyDraftIsStale
                        ? `Ontology conflict, ${committableDraftCount} committable changes`
                        : behindDraftCount > 0
                          ? `Sync ${committableDraftCount} committable changes, ${behindDraftCount} behind`
                          : undefined
                  }
                  onClick={() => void syncHumanDraft()}
                >
                  {syncingDraft ? (
                    <LoaderCircle className="spin" size={14} />
                  ) : ontologyDraftIsStale ? (
                    <AlertTriangle size={14} />
                  ) : (
                    <CloudUpload size={14} />
                  )}
                  <span>Sync</span>
                  {committableDraftCount > 0 && <small>{committableDraftCount}</small>}
                </button>
                {behindDraftCount > 0 && (
                  <span className="draft-behind-count" role="status">
                    Behind <small>{behindDraftCount}</small>
                  </span>
                )}
              </div>
              <button
                className="button secondary"
                disabled={projectReconciliation !== "authoritative"}
                onClick={() => {
                  const chatId = startConversation("project_chat");
                  openChats(chatId);
                }}
              >
                <MessageCircle size={14} /> Ask
              </button>
            </div>
            <div
              className="project-header-group project-utility-group"
              role="group"
              aria-label="Project utilities"
            >
              <button
                className="icon-button task-history-control"
                aria-label={activeTask ? "Project history, task in progress" : "Project history"}
                onClick={() => {
                  setHistorySummariesRevision(null);
                  setHistorySummariesError(null);
                  setProjectHistoryOpen(true);
                }}
              >
                <History size={15} />
                {activeTask ? <span className="activity-pulse" /> : null}
              </button>
              <button
                className="icon-button primary refresh-control"
                disabled={
                  mutationsDisabled ||
                  projectReconciliation !== "authoritative" ||
                  !project.canonical_state.reachable ||
                  taskStarting
                }
                aria-label={runKind === "seed" ? "Seed project" : "Refresh project"}
                onClick={() => setRunDialogOpen(true)}
              >
                <RefreshCw
                  className={activeTask && activeTask.status !== "pausing" ? "spin" : ""}
                  size={15}
                />
              </button>
            </div>
          </div>
        </header>
      )}

      <nav className="project-tabs" aria-label="Project panels">
        {projectHeaderCollapsed && (
          <>
            <button
              className="project-tabs-back project-back"
              onClick={returnToProjects}
              aria-label="All projects"
            >
              <ArrowLeft size={16} />
            </button>
            <ProjectDock
              className="project-tabs-project-dock"
              tabs={openProjectTabs}
              activeProjectId={projectId}
              onActivate={activateProjectTab}
              onClose={closeDockedProject}
            />
          </>
        )}
        <button
          aria-expanded={!projectHeaderCollapsed}
          aria-controls={!projectHeaderCollapsed ? "project-header-actions" : undefined}
          aria-label={projectHeaderCollapsed ? "Expand project header" : "Collapse project header"}
          className="project-tabs-toggle"
          title={projectHeaderCollapsed ? "Expand project header" : "Collapse project header"}
          onClick={() => setProjectHeaderCollapsed((collapsed) => !collapsed)}
        >
          {projectHeaderCollapsed ? <ChevronDown size={15} /> : <ChevronUp size={15} />}
        </button>
        {navItems.map((item) => (
          <button
            key={item.view}
            className={
              view === item.view || (item.view === "scientific" && view === "dag") ? "active" : ""
            }
            onClick={() =>
              item.view === "chats"
                ? openChats()
                : changeView(item.view === "scientific" ? researchSubviewRef.current : item.view)
            }
          >
            {item.icon}
            <span>{item.label}</span>
            {item.view === "attention" && <small className="inbox-count">{attentionCount}</small>}
            {item.view === "paper" && paper.sync_state !== "synced" && <small>1</small>}
            {item.view === "chats" && chatsIndicator && (
              <small
                className={`chats-indicator ${chatsIndicator}`}
                aria-label={chatsIndicator === "active" ? "Chat task active" : "Unread chat result"}
              >
                {chatsIndicator === "active" ? "•" : unreadChatTaskIds.size}
              </small>
            )}
          </button>
        ))}
        {showTrustFilter && (
          <label className="trust-filter">
            <span>Show</span>
            <select
              value={trustView}
              onChange={(event) => setTrustView(event.target.value as TrustView)}
            >
              <option value="working">Working graph</option>
              <option value="accepted">Accepted only</option>
              <option value="review">Everything</option>
            </select>
          </label>
        )}
      </nav>

      {dockedNodes.length > 0 && (
        <section className="node-window-dock" aria-label="Docked node windows">
          <div className="node-window-dock-label">
            <Network size={14} />
            <span>Docked nodes</span>
          </div>
          <div className="node-window-dock-items">
            {dockedNodes.map(({ nodeId, node }) => (
              <button
                className="node-window-dock-item"
                key={nodeId}
                type="button"
                aria-label={`Restore ${node.title} node window`}
                onClick={() => restoreDockedNode(nodeId)}
              >
                <span className={`node-window-dock-state ${node.standing}`} />
                <span>{node.title}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      <div className="project-notices">
        {updateSurface}
        {!project.canonical_state.reachable && (
          <div className="coverage-banner state-offline">
            <AlertTriangle size={15} />
            <span>
              <strong>Canonical state is offline.</strong> Sync is unavailable.
            </span>
          </div>
        )}
        {replayWarning && (
          <div className="coverage-banner replay-degraded" role="alert">
            <AlertTriangle size={15} />
            <span>
              <strong>Replay degraded.</strong> {replayWarning}
            </span>
          </div>
        )}
        {shouldShowCoverageBoundaryWarning(project) && view === "overview" && (
          <div className="coverage-banner">
            <AlertTriangle size={15} />
            <span>
              <strong>Coverage boundary:</strong> {project.coverage.note}
            </span>
          </div>
        )}
        {rejectedPatches.length > 0 && view === "attention" && (
          <div className="coverage-banner validation-rejected" role="status">
            <AlertTriangle size={15} />
            <span>
              <strong>
                History note: {rejectedPatches.length} operation
                {rejectedPatches.length === 1 ? "" : "s"} rejected and not applied.
              </strong>{" "}
              RCP kept the attempted patch for audit, so the graph was unchanged. This is not an
              Inbox decision. Reason: {rejectedPatches.at(-1)?.message}
            </span>
            <button
              type="button"
              className="icon-button compact"
              aria-label="Dismiss history note"
              onClick={() => dismissHistoryNotices(rejectedPatches)}
            >
              <X size={14} />
            </button>
          </div>
        )}
      </div>

      <main
        className={view === "paper" ? "project-panel paper" : "project-panel"}
        ref={panelRef}
        inert={projectReconciliation !== "authoritative"}
        aria-busy={projectReconciliation !== "authoritative"}
      >
        <Suspense
          fallback={
            <div className="project-view-loading" aria-label="Loading view">
              <LoaderCircle className="spin" />
            </div>
          }
        >
          {(view === "scientific" || view === "dag") && (
            <div className="research-subpanel" role="tablist" aria-label="Research panels">
              <button
                type="button"
                role="tab"
                aria-selected={view === "scientific"}
                className={view === "scientific" ? "active" : ""}
                onClick={() => changeView("scientific")}
              >
                <GitBranch size={13} /> Research
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={view === "dag"}
                className={view === "dag" ? "active" : ""}
                onClick={() => changeView("dag")}
              >
                <Network size={13} /> DAG
              </button>
            </div>
          )}
          {view === "overview" && (
            <ProjectOverview
              project={projectWithGraph(project, presentedGraph)}
              graph={presentedGraph}
              decisionsAwaitingChoice={attentionDecisions}
              latestRevisionSummary={
                latestRevisionSummary?.to_revision === graph.revision ? latestRevisionSummary : null
              }
              onNavigate={changeView}
            />
          )}
          {view === "attention" && (
            <div className="attention-page">
              <div className="attention-main">
                <AttentionOverview graph={graph} onSelectNode={openNode} />
                <ProposalJudgmentSection
                  proposals={pendingProposals}
                  glossaryIndex={glossaryIndex}
                  draft={mutationsDisabled ? null : humanDraft}
                  mutationsDisabled={mutationsDisabled}
                  onDecision={(proposal, decision) =>
                    updateHumanDraft((draft) =>
                      stageProposalDecision(draft, graph, proposal.id, decision),
                    )
                  }
                />
              </div>
              <AttentionRail
                decisions={attentionDecisions}
                blockers={openBlockers}
                onSelectNode={openNodeById}
              />
            </div>
          )}
          {view === "scientific" && (
            <ScientificView
              graph={presentedGraph}
              trustView={trustView}
              mutationsDisabled={mutationsDisabled}
              onSelectNode={openNode}
              onStageCustomNode={(node) =>
                updateHumanDraft((draft) => stageCustomNode(draft, node))
              }
            />
          )}
          {view === "dag" && (
            <DagView
              graph={presentedGraph}
              trustView={trustView}
              projectId={project.id}
              viewportRef={activeDagViewportRef!}
              relationFocusNodeId={dagRelationFocusId}
              onClearRelationFocus={() => setDagRelationFocusId(null)}
              onSelectNode={openNode}
            />
          )}
          {view === "execution" && (
            <ExecutionView
              graph={presentedGraph}
              attentionBlockerIds={attentionBlockerIds}
              tasks={tasks}
              watchers={watchers}
              experimentControl={project.experiment_control}
              dismissedTaskIds={dismissedTaskIds}
              selectedExperimentId={selectedExperimentRunId}
              focusExperimentId={focusExperimentRunId}
              runBusy={taskStarting}
              stopBusyId={experimentStopId}
              taskActionId={taskActionId}
              providerLabels={Object.fromEntries(
                Object.entries(project.providers).map(([id, provider]) => [
                  id,
                  provider.label || id,
                ]),
              )}
              mutationsDisabled={mutationsDisabled}
              onInspectTask={setTaskInspectorId}
              onDismissTask={dismissTaskNotification}
              onSelectNode={openNode}
              onSelectExperiment={setSelectedExperimentRunId}
              onDetailFocused={() => setFocusExperimentRunId(null)}
              onRunExperiment={(node) => void runExperiment(node)}
              onStopExperiment={(nodeId) => void stopExperimentLoop(nodeId)}
              onRecoverExperiment={(task, action) => void operateTask(task, action, false)}
              onSwitchExperimentProvider={setRetryTask}
            />
          )}
          {view === "paper" && (
            <PaperWorkspace
              key={project.id}
              apiBase={apiBase}
              project={project}
              initialPaper={paper}
              tasks={tasks}
              onStartTask={startAgentTask}
              onPaperChange={updatePaper}
            />
          )}
          {view === "settings" && (
            <ProjectSettings
              apiBase={apiBase}
              project={project}
              usage={usage}
              onRefreshUsage={refreshUsage}
              cacheClearDisabled={Boolean(activeTask)}
              writesDisabled={mutationsDisabled}
              showDisplaySettings={desktop}
              textScale={textScale}
              onTextScaleChange={changeAppTextScale}
              identity={actorIdentity}
              identityError={actorIdentityError}
              onIdentitySaved={setActorIdentity}
              onRefreshReadiness={refreshReadiness}
              onCacheMetricsChange={(cacheMetrics) => {
                setProject((current) =>
                  current ? { ...current, cache_metrics: cacheMetrics } : current,
                );
              }}
              onSaved={(saved, preserveReadiness = true) => {
                setProject((current) =>
                  preserveReadiness ? preserveProjectReadiness(saved, current) : saved,
                );
                setRunScope(saved.default_run_truth_scope);
                setNotice({ kind: "info", text: "Project defaults synced." });
              }}
            />
          )}
          {view === "chats" && (
            <ChatsWorkspace
              project={project}
              conversations={conversations}
              selectedChatId={selectedChatId}
              nodes={presentedGraph.nodes}
              glossaryIndex={glossaryIndex}
              runScope={runScope}
              tasks={tasks}
              watchers={watchers}
              graphChangesDisabled={mutationsDisabled}
              unreadTaskIds={unreadChatTaskIds}
              chatTranscripts={chatTranscripts}
              hasMore={chatSummaryNextOffset < chatSummaryTotal}
              loadingMore={chatSummariesLoading}
              onSelect={selectChat}
              onOpenNode={openNodeById}
              onLoadMore={() => void loadMoreChatSummaries()}
              onStartTask={startAgentTask}
              onResumeTask={(task) => void operateTask(task, "resume")}
              onRetryTask={requestRetry}
              onInspectTask={setTaskInspectorId}
              onOpenInbox={() => changeView("attention")}
              onRepairGraphUpdate={repairGraphUpdate}
              onStopWatcher={(watcherId) => void stopWatcher(watcherId)}
              onNewSession={(conversation) => {
                const node = conversation.nodeId
                  ? (presentedGraph.nodes[conversation.nodeId] ?? null)
                  : null;
                selectChat(startConversation(conversation.kind, node));
              }}
            />
          )}
        </Suspense>
      </main>

      {(
        [
          { slot: "original" as const, selected: selectedNode },
          { slot: "companion" as const, selected: companionNode },
        ] satisfies Array<{ slot: DetailWindowSlot; selected: GraphNode | null }>
      ).map(({ slot, selected }) => {
        if (!selected) return null;
        const node = presentedGraph.nodes[selected.id] ?? selected;
        const experimentControl = experimentControlForNode(node);
        return (
          <DetailDrawer
            key={`${slot}:${node.id}`}
            node={node}
            edges={Object.values(presentedGraph.edges)}
            allNodes={presentedGraph.nodes}
            glossaryIndex={glossaryIndex}
            beliefTransitions={graph.belief_transitions}
            validationMessages={graph.validation_messages}
            ontology={presentedGraph.ontology}
            sizeStorageKey={nodeDetailSizeStorageKey(project.id)}
            detailSlot={slot}
            focusRequestToken={detailFocusTokens[slot]}
            mutationsDisabled={mutationsDisabled}
            stagedNewNode={Boolean(humanDraft?.custom_nodes[node.id])}
            stagedForRemoval={Boolean(humanDraft?.removed_node_ids.includes(node.id))}
            hasStagedNodeChange={Boolean(humanDraft?.nodes[node.id])}
            draftNodeChange={humanDraft?.nodes[node.id]}
            canonicalNode={graph.nodes[node.id]}
            behind={draftNodeIsBehind(humanDraft?.nodes[node.id], graph.nodes[node.id])}
            canonicalStanding={graph.nodes[node.id]?.standing ?? node.standing}
            experimentControl={experimentControl}
            experimentRunDisabled={false}
            experimentRunBusy={taskStarting}
            decisionChoiceStaged={Boolean(
              humanDraft?.nodes[node.id]?.changes.selected_option !== undefined ||
              humanDraft?.nodes[node.id]?.changes.status === "decided",
            )}
            onUnstage={() => {
              updateHumanDraft((draft) => unstageCustomNode(draft, node.id));
              closeDetailSlot(slot);
            }}
            onRemove={() =>
              updateHumanDraft((draft) =>
                stageNodeRemoval(draft, graph, node.id, Boolean(experimentControl?.active)),
              )
            }
            onUndoRemoval={() => updateHumanDraft((draft) => unstageNodeRemoval(draft, node.id))}
            onClose={() => closeDetailSlot(slot)}
            onDock={() => dockNode(node.id, slot)}
            onBeginEdit={() =>
              updateHumanDraft((draft) => stageNodeEditStart(draft, graph, node.id))
            }
            onStanding={(standing) =>
              updateHumanDraft((draft) => stageNodeStanding(draft, graph, node.id, standing))
            }
            onStage={(changes) =>
              updateHumanDraft((draft) => stageNodeEdit(draft, graph, node.id, changes))
            }
            onApplyField={(changes, fieldKey) =>
              updateHumanDraft((draft) => stageNodeEdit(draft, graph, node.id, changes, [fieldKey]))
            }
            onDecisionChoice={(selectedOption) =>
              updateHumanDraft((draft) =>
                stageDecisionChoice(draft, graph, node.id, selectedOption),
              )
            }
            onRunExperiment={() => void runExperiment(node)}
            onOpenChat={() => {
              const chatId = ensureConversation("node_chat", node);
              selectChat(chatId);
              setFloatingChat({ chatId, nodeId: node.id });
            }}
            onOpenRelatedNode={(nodeId) => openRelatedNode(slot, nodeId)}
            onSelectNode={openNodeById}
          />
        );
      })}
      {floatingChat && (
        <DraggableWindow className="node-chat-window" kind="chat" resizable>
          <Suspense
            fallback={
              <div className="project-view-loading" aria-label="Loading chat">
                <LoaderCircle className="spin" />
              </div>
            }
          >
            <NodeChat
              key={floatingChat.chatId}
              project={project}
              node={presentedGraph.nodes[floatingChat.nodeId] ?? null}
              nodes={presentedGraph.nodes}
              glossaryIndex={glossaryIndex}
              conversationTitle={
                conversations.find((conversation) => conversation.chatId === floatingChat.chatId)
                  ?.title
              }
              runScope={runScope}
              tasks={tasks}
              watchers={watchers}
              historyMessages={chatTranscripts.get(floatingChat.chatId)?.messages}
              chatId={floatingChat.chatId}
              presentation="floating"
              graphChangesDisabled={mutationsDisabled}
              onStartTask={startAgentTask}
              onResumeTask={(task) => void operateTask(task, "resume")}
              onRetryTask={requestRetry}
              onInspectTask={setTaskInspectorId}
              onOpenInbox={() => {
                setFloatingChat(null);
                changeView("attention");
              }}
              onRepairGraphUpdate={repairGraphUpdate}
              onOpenNode={openNodeById}
              onStopWatcher={(watcherId) => void stopWatcher(watcherId)}
              onNewSession={() => {
                const node = presentedGraph.nodes[floatingChat.nodeId] ?? null;
                const chatId = startConversation("node_chat", node);
                selectChat(chatId);
                setFloatingChat({ chatId, nodeId: floatingChat.nodeId });
              }}
              onClose={() => setFloatingChat(null)}
            />
          </Suspense>
        </DraggableWindow>
      )}
      <RunDialog
        open={runDialogOpen}
        kind={runKind}
        project={project}
        initialScope={runScope}
        busy={taskStarting}
        onClose={() => setRunDialogOpen(false)}
        onRun={(config, scope, message) => void runAgent(config, scope, message)}
      />
      {retryTask && retryConfig && (
        <RunDialog
          open
          mode="retry"
          kind={
            isExperimentLoopRecovery(retryTask)
              ? "node_chat"
              : retryTask.kind === "seed"
                ? "seed"
                : "refresh"
          }
          project={project}
          initialScope={retryTask.request.run_truth_scope || project.default_run_truth_scope}
          initialConfig={retryConfig}
          busy={taskActionId === retryTask.operation_id}
          onClose={() => setRetryTask(null)}
          onRun={(config) => void retryAgentTask(retryTask, config)}
        />
      )}
      {projectHistoryOpen && (
        <ProjectHistoryDrawer
          summaries={historyRevisionSummaries}
          tasks={tasks}
          loading={historySummariesRevision !== graph.revision}
          error={historySummariesError}
          onInspectTask={(taskId) => {
            setProjectHistoryOpen(false);
            setTaskInspectorId(taskId);
          }}
          onClose={() => setProjectHistoryOpen(false)}
        />
      )}
      {taskInspectorId && (
        <AgentTaskInspector
          tasks={tasks}
          task={inspectedTask}
          loading={taskInspectorLoading}
          actionBusy={Boolean(
            taskActionId || (activeTask && activeTask.operation_id !== taskInspectorId),
          )}
          mutatingActionsDisabled={Boolean(
            mutationsDisabled && inspectedTask && taskMayMutateGraph(inspectedTask),
          )}
          onSelect={setTaskInspectorId}
          onPause={() => inspectedTask && void operateTask(inspectedTask, "pause")}
          onResume={() => inspectedTask && void operateTask(inspectedTask, "resume")}
          onRetry={() => inspectedTask && requestRetry(inspectedTask)}
          onDismiss={() => inspectedTask && dismissTaskNotification(inspectedTask.operation_id)}
          onClose={() => setTaskInspectorId(null)}
        />
      )}
      {notice && (
        <button className={`toast ${notice.kind}`} onClick={() => setNotice(null)}>
          {notice.text}
        </button>
      )}
      {desktopAccessSurface}
      {actorNameSurface}
    </div>
  );
}

function readDismissedTaskIds(projectId: string | null): Set<string> {
  try {
    return parseDismissedTaskIds(localStorage.getItem(taskNotificationStorageKey(projectId)));
  } catch {
    return new Set();
  }
}

function readTrustView(): TrustView {
  try {
    return (localStorage.getItem("rcp:trust-view") as TrustView) || "working";
  } catch {
    return "working";
  }
}

function readTextScale(): number {
  try {
    return normalizeTextScale(localStorage.getItem(TEXT_SCALE_STORAGE_KEY));
  } catch {
    return normalizeTextScale(null);
  }
}

function projectHeaderCollapsedStorageKey(projectId: string): string {
  return `${PROJECT_HEADER_COLLAPSED_KEY}:${projectId}`;
}

function readProjectHeaderCollapsed(projectId: string | null): boolean {
  if (!projectId) return false;
  try {
    return localStorage.getItem(projectHeaderCollapsedStorageKey(projectId)) === "true";
  } catch {
    return false;
  }
}

function historyNoticeStorageKey(projectId: string | null): string {
  return `rcp:dismissed-history-notices:${projectId ?? "none"}`;
}

function readDismissedHistoryNoticeIds(projectId: string | null): Set<string> {
  try {
    return parseDismissedTaskIds(localStorage.getItem(historyNoticeStorageKey(projectId)));
  } catch {
    return new Set();
  }
}

function validationNoticeId(message: ValidationMessage): string {
  return JSON.stringify([message.code, message.patch_revision ?? null, message.message]);
}

function projectWithGraph(project: ProjectSnapshot, graph: GraphState): ProjectSnapshot {
  const standingCounts = Object.values(graph.nodes).reduce(
    (counts, node) => {
      counts[node.standing] += 1;
      return counts;
    },
    { asserted: 0, accepted: 0, contested: 0 },
  );
  return {
    ...project,
    graph,
    revision: graph.revision,
    primary_question: project.primary_question
      ? (graph.nodes[project.primary_question.id] ?? project.primary_question)
      : project.primary_question,
    counts: { ...project.counts, ...standingCounts },
  };
}

function preserveProjectReadiness(
  next: ProjectSnapshot,
  current: ProjectSnapshot | null,
): ProjectSnapshot {
  if (!current || current.id !== next.id) return next;
  return {
    ...next,
    provider_readiness: current.provider_readiness,
    providers: current.providers,
    provider_skill_inventories: current.provider_skill_inventories,
  };
}

function taskRetryConfig(task: AgentTask, project: ProjectSnapshot): AgentRunConfig {
  const profileKind =
    task.kind === "seed" ? "seed" : task.kind === "refresh" ? "refresh" : "node_chat";
  const profile = project.agent_profiles[profileKind];
  return {
    provider: task.request.provider || profile.provider,
    model: task.request.model ?? profile.model,
    reasoning: task.request.reasoning || profile.reasoning,
    run_on: task.request.run_on || profile.run_on,
  };
}

function isExperimentLoopRecovery(task: AgentTask): boolean {
  return task.request.patch_kind === "experiment_loop";
}

export function taskRetryRequestBody(
  task: AgentTask,
  config: AgentRunConfig,
): AgentRunConfig | Omit<AgentRunConfig, "run_on"> {
  if (!isExperimentLoopRecovery(task)) return config;
  return {
    provider: config.provider,
    model: config.model,
    reasoning: config.reasoning,
  };
}

export function projectIdFromHash(hash = window.location.hash): string | null {
  return parseProjectHash(hash).projectId;
}

function isSetupRoute(): boolean {
  return window.location.hash === "#/projects/new";
}

interface DesktopUpdateNoticeProps {
  update: DesktopUpdate | null;
  activeWork: boolean;
  expanded: boolean;
  applying: boolean;
  error: string | null;
  onExpand: () => void;
  onApply: () => void;
  onDismiss: () => void;
}

function DesktopUpdateNotice({
  update,
  activeWork,
  expanded,
  applying,
  error,
  onExpand,
  onApply,
  onDismiss,
}: DesktopUpdateNoticeProps) {
  if (update && activeWork && !expanded && !error) {
    return (
      <button className="desktop-update-marker" type="button" onClick={onExpand}>
        <CircleArrowUp size={13} /> Update ready
      </button>
    );
  }
  return (
    <div
      className={`desktop-update-notice${error ? " error" : ""}`}
      role={error ? "alert" : "status"}
    >
      <CircleArrowUp size={15} />
      <strong>{error || `RCP ${update?.version || "update"} is ready`}</strong>
      {update && (
        <button className="button secondary" type="button" disabled={applying} onClick={onApply}>
          {applying ? <LoaderCircle className="spin" size={13} /> : null}
          {activeWork ? "Update now" : "Update"}
        </button>
      )}
      <button className="desktop-update-dismiss" type="button" onClick={onDismiss}>
        Later
      </button>
    </div>
  );
}
