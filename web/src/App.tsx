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
  Telescope,
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
import { isActiveTask } from "./agentTasks";
import { chatIndicator, chatEntryConversationId, groupChatConversations } from "./chatWorkspace";
import {
  api,
  ApiError,
  loadProjectReadiness,
  reauthorizeEpisode,
  sendEpisodeMessage,
  startEpisode,
  stopEpisode,
} from "./api";
import {
  backendReconnectLabel,
  desktopShowReady,
  setDesktopWebviewZoom,
  isDesktopRuntime,
  listenDesktopEvent,
  type DesktopUpdate,
} from "./desktopRuntime";
import { graphMutationsDisabled, replayFailureLabel, taskMayMutateGraph } from "./graphAuthority";
import { buildGlossaryIndex } from "./glossary";
import { parseProjectHash } from "./experimentBoard";
import { nodeDetailSizeStorageKey, type DetailWindowSlot } from "./floatingWindow";
import { episodeReportPreviewUrl } from "./campaigns";
import {
  cloneAgentTasksSnapshot,
  useAgentTasks,
  type AgentTasksSnapshot,
} from "./hooks/useAgentTasks";
import { useActorIdentity } from "./hooks/useActorIdentity";
import {
  cloneChatStateSnapshot,
  useChatState,
  visibleChatTranscriptIds,
  visibleUnreadChatId,
  type ChatStateSnapshot,
} from "./hooks/useChatState";
import { useDesktopShell } from "./hooks/useDesktopShell";
import { useEpisodeDialogs } from "./hooks/useEpisodeDialogs";
import { useGraphSelection, type GraphSelectionTabSnapshot } from "./hooks/useGraphSelection";
import {
  cloneProjectHistorySnapshot,
  useProjectHistory,
  validationNoticeId,
  type ProjectHistorySnapshot,
} from "./hooks/useProjectHistory";
import { startProjectCachePolling, useProjectTabs } from "./hooks/useProjectTabs";
import { AutoResearchDialog } from "./components/AutoResearchDialog";
import { AgentTaskInspector } from "./components/AgentTaskInspector";
import { AttentionRail, ProposalJudgmentSection } from "./components/AttentionRail";
import { AutoResearchEpisodes } from "./components/CampaignRuns";
import { DetailDrawer } from "./components/DetailDrawer";
import { DraggableWindow } from "./components/DraggableWindow";
import { ProjectHistoryDrawer } from "./components/ProjectHistoryDrawer";
import { ProjectDock } from "./components/ProjectDock";
import { RunDialog } from "./components/RunDialog";
import { TeamLoginBoundary } from "./components/TeamLoginBoundary";
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
  reconcileHumanDraft,
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
  type HumanSyncRequest,
} from "./humanDraft";
import type {
  AgentRunConfig,
  AgentTask,
  AgentTaskKind,
  AgentTaskRequest,
  AgentUsageSnapshot,
  AppView,
  ExperimentControlState,
  GraphNode,
  GraphState,
  Health,
  PaperSnapshot,
  ProjectCard,
  ProjectInvitation,
  ProjectSnapshot,
  TrustView,
  WatcherRecord,
} from "./types";
import { decodeGraphState, decodeProjectSnapshot, DISPLAY_NAME_MAX_LENGTH } from "./types";
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

export { revisionSummariesUrl } from "./hooks/useProjectHistory";
export {
  shouldLoadVisibleChatTranscript,
  visibleChatTranscriptIds,
  visibleUnreadChatId,
} from "./hooks/useChatState";
export {
  LIVE_EPISODE_POLL_INTERVAL_MS,
  resultViewLoadIsCurrent,
  resultViewSelectionIsCurrent,
  resultViewSelectionKey,
  startLiveEpisodePolling,
} from "./hooks/useEpisodeDialogs";
export { relatedNodeWindowAction } from "./hooks/useGraphSelection";
export {
  ACTIVE_PROJECT_CACHE_OBSERVE_INTERVAL_MS,
  OPEN_PROJECT_HEARTBEAT_INTERVAL_MS,
  PROJECT_TAB_CACHE_LIMIT,
  cacheProjectTabState,
  inactiveProjectTabState,
  projectIdsForCacheHeartbeat,
  projectTabStateForOpen,
  singleFlightProjectCacheHeartbeat,
  startProjectCachePolling,
} from "./hooks/useProjectTabs";
import { initialProjectHash, isEditableShortcutTarget, projectTabShortcut } from "./projectTabs";

const PROVIDER_SKILL_READINESS_POLL_DELAY_MS = 1_000;
const PROVIDER_SKILL_READINESS_MAX_FOLLOW_UPS = 20;

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

const navItems: Array<{ view: AppView; label: string; icon: React.ReactNode }> = [
  { view: "overview", label: "Overview", icon: <LayoutList size={14} /> },
  { view: "attention", label: "Inbox", icon: <Inbox size={14} /> },
  { view: "scientific", label: "Research", icon: <GitBranch size={14} /> },
  { view: "execution", label: "Runs", icon: <FlaskConical size={14} /> },
  { view: "paper", label: "Paper", icon: <FileText size={14} /> },
  { view: "settings", label: "Settings", icon: <Settings2 size={14} /> },
  { view: "chats", label: "Chats", icon: <MessageCircle size={14} /> },
];

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

export async function projectIsStillReadable(
  fetchJson: <T>(path: string) => Promise<T>,
  projectId: string,
): Promise<boolean> {
  // The project index is already filtered to what the caller may see, so its
  // answer covers both a deleted project and one that is no longer ours.
  // A failure to ask is not an answer: keep the tab.
  try {
    const cards = await fetchJson<ProjectCard[]>("/api/projects");
    return cards.some((card) => card.id === projectId);
  } catch {
    return true;
  }
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

const RESULT_VIEW_TERMINAL_STATUSES = new Set(["succeeded", "failed", "interrupted"]);

type ProjectReconciliation = "opening" | "reconciling" | "authoritative" | "failed";

interface CachedProjectTabState
  extends ProjectHistorySnapshot, AgentTasksSnapshot, ChatStateSnapshot, GraphSelectionTabSnapshot {
  project: ProjectSnapshot;
  projectHeaderCollapsed: boolean;
  humanDraft: HumanDraft | null;
  draftReconciliationDiscardedProposalIds?: string[];
  usage: AgentUsageSnapshot | null;
  watchers: WatcherRecord[];
}

export function cachedSnapshotCanReplace(
  renderedProjectId: string | null,
  renderedRevision: number,
  snapshot: ProjectSnapshot,
): boolean {
  return snapshot.id !== renderedProjectId || snapshot.graph.revision >= renderedRevision;
}

export function reconcileInactiveProjectTabState(
  state: CachedProjectTabState,
  snapshot: ProjectSnapshot,
): CachedProjectTabState {
  const decodedSnapshot = decodeProjectSnapshot(snapshot);
  if (
    !cachedSnapshotCanReplace(state.project.id, state.project.graph.revision, decodedSnapshot) ||
    decodedSnapshot.id !== state.project.id
  )
    return state;
  if (decodedSnapshot.snapshot_freshness !== "fresh") return state;
  const reconciliation = state.humanDraft
    ? reconcileHumanDraft(state.humanDraft, decodedSnapshot.graph)
    : null;
  const rebased = reconciliation?.draft ?? null;
  const retainedDraft = rebased && humanDraftChangeCount(rebased) > 0 ? rebased : null;
  const presented = applyHumanDraft(decodedSnapshot.graph, retainedDraft);
  return {
    ...state,
    project: decodedSnapshot,
    selectedNodeId:
      state.selectedNodeId && presented.nodes[state.selectedNodeId] ? state.selectedNodeId : null,
    companionNodeId:
      state.companionNodeId && presented.nodes[state.companionNodeId]
        ? state.companionNodeId
        : null,
    floatingChat:
      state.floatingChat && presented.nodes[state.floatingChat.nodeId] ? state.floatingChat : null,
    humanDraft: retainedDraft,
    draftReconciliationDiscardedProposalIds: [
      ...new Set([
        ...(state.draftReconciliationDiscardedProposalIds ?? []),
        ...(reconciliation?.discardedProposalIds ?? []),
      ]),
    ],
  };
}

export function proposalChoicesClearedNotice(proposalIds: string[]): string {
  return `Externally resolved proposal choices were cleared: ${proposalIds.join(", ")}.`;
}

export function humanSyncSuccessNotice(
  revision: number,
  submittedProposals: HumanSyncRequest["proposals"],
  nextGraph: GraphState,
): string {
  const withdrawnProposalIds = submittedProposals
    .filter((judgment) => nextGraph.proposals[judgment.proposal_id]?.status === "withdrawn")
    .map((judgment) => judgment.proposal_id)
    .sort();
  return withdrawnProposalIds.length > 0
    ? `Synced revision ${revision}. Stale proposals were withdrawn and their proposed changes were not applied: ${withdrawnProposalIds.join(", ")}.`
    : `Synced revision ${revision}.`;
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
  const {
    identityReady,
    identityIssue,
    verifiedHealth,
    actorIdentity,
    actorIdentityError,
    actorIdentityChecked,
    teamSessionRequired,
    actorNamePromptOpen,
    actorNameDraft,
    actorNameSaving,
    actorNameError,
    requestActorName,
    settleActorNamePrompt,
    saveActorName,
    authenticateTeamSession: authenticateIdentityTeamSession,
    reportIdentityIssue,
    reverifyIdentity,
    currentActiveAgentTasks,
    updateActorNameDraft,
  } = useActorIdentity();
  const {
    reconnecting,
    desktopUpdate,
    updateExpanded,
    updateApplying,
    updateError,
    pendingDesktopProject,
    desktopAccessError,
    refreshDesktopUpdate,
    recordDesktopUpdateReady,
    requestDesktopProjectOpen,
    continueDesktopProjectOpen: continueDesktopProjectAccess,
    dismissDesktopProjectOpen,
    reconnectBackend: reconnectDesktopBackend,
    applyUpdate: applyDesktopShellUpdate,
    expandUpdate,
    dismissUpdate,
  } = useDesktopShell(desktop);
  const [notice, setNotice] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const reportErrorNotice = useCallback((text: string) => {
    setNotice({ kind: "error", text });
  }, []);
  const [projectInvitations, setProjectInvitations] = useState<ProjectInvitation[]>([]);
  const refreshProjectInvitations = useCallback(async () => {
    try {
      setProjectInvitations(await api<ProjectInvitation[]>("/api/project-invitations"));
    } catch {
      // Invitations are additive to the index; failing to read them must not
      // stop the projects you already have from rendering.
    }
  }, []);
  const {
    projectId,
    setupOpen,
    projects,
    openProjectTabs,
    experimentLoops,
    project,
    projectHeaderCollapsed,
    isActiveProject,
    getActiveProjectId,
    replaceProject,
    updateProject,
    replaceProjects,
    loadProjectIndex,
    applyHashRoute,
    clearProjectRoute,
    openSetup,
    returnToProjects: returnToProjectIndex,
    commitProjectOpen: commitProjectRoute,
    activateProjectTab: activateProjectRoute,
    closeDockedProject: closeProjectRoute,
    removeProject,
    resetProjectHeader,
    restoreProjectHeader,
    toggleProjectHeader,
    cacheProjectState,
    cachedProjectStateForOpen,
    inactiveCachedProjectState,
    isProjectTabOpen,
    projectIdsForHeartbeat,
    adjacentProjectId,
    runProjectHeartbeat,
  } = useProjectTabs<CachedProjectTabState>({
    initialProjectId: initialRoute.project.projectId,
    initialSetupOpen: initialRoute.setupOpen,
    projectIndexReady:
      identityReady && !identityIssue && actorIdentityChecked && !teamSessionRequired,
    reportError: reportErrorNotice,
  });
  const [textScale, setTextScale] = useState(readTextScale);
  const [loading, setLoading] = useState(true);
  const [projectReconciliation, setProjectReconciliation] =
    useState<ProjectReconciliation>("opening");
  const [humanDraft, setHumanDraft] = useState<HumanDraft | null>(null);
  const [syncingDraft, setSyncingDraft] = useState(false);
  const [usage, setUsage] = useState<AgentUsageSnapshot | null>(null);
  const [watchers, setWatchers] = useState<WatcherRecord[]>([]);
  const {
    graph,
    paper,
    view,
    trustView,
    runScope,
    selectedNode,
    companionNode,
    detailFocusTokens,
    selectedExperimentRunId,
    focusExperimentRunId,
    experimentStopId,
    watcherCheckId,
    dockedNodeIds,
    dagRelationFocusId,
    panelRef,
    activeDagViewportRef,
    captureProjectSelection,
    restoreProjectSelection,
    resetProjectSelection,
    applyCanonicalProject,
    applySyncedGraph,
    replacePaper,
    replaceRunScope,
    applyRouteSelection,
    changeView,
    openLastResearchView,
    changeTrustView,
    openNode,
    openRelatedNode: openRelatedGraphNode,
    closeDetailSlot,
    clearNodeSelections,
    dockNode,
    restoreDockedNode: restoreDockedGraphNode,
    selectExperiment,
    clearExperimentFocus,
    showExperiment,
    beginExperimentStop,
    beginWatcherCheck,
    clearDagRelationFocus,
    forgetProjectViewport,
  } = useGraphSelection({
    initialView: initialRoute.project.view,
    initialExperimentId: initialRoute.project.experimentId,
    projectId,
    loadedProjectId: project?.id ?? null,
    loading,
  });
  const authoritativeProjectId = useRef<string | null>(null);
  const reloadRef = useRef<(includeTasks?: boolean) => Promise<void>>(async () => undefined);
  const authoritativeReloadInFlight = useRef<{
    projectId: string;
    request: Promise<void>;
  } | null>(null);
  const renderedRevisionRef = useRef(graph.revision);
  const initialShowHandshake = useRef(false);
  const readinessRequestedProjectIds = useRef(new Set<string>());
  const providerSkillReadinessPoll = useRef<{ projectId: string; timeoutId: number } | null>(null);
  const currentProjectStateRef = useRef<Omit<CachedProjectTabState, "viewState"> | null>(null);
  renderedRevisionRef.current = graph.revision;
  const apiBase = projectId ? `/api/projects/${encodeURIComponent(projectId)}` : "";
  const {
    snapshot: agentTasksSnapshot,
    taskStarting,
    taskActionId,
    taskInspectorLoading,
    activeTask,
    activityTask,
    replaceTasks,
    upsertTask,
    recordStartedTask,
    presentTask: presentAgentTask,
    selectTaskInspector,
    chooseRetryTask,
    closeRetryTask,
    dismissTaskNotification,
    beginTaskStart,
    beginTaskAction,
    beginTaskRepair,
    resetProjectTasks,
    restoreProjectTasks,
  } = useAgentTasks({ projectId, reportError: reportErrorNotice });
  const { retryTask, tasks, taskInspectorId, inspectedTask, dismissedTaskIds } = agentTasksSnapshot;
  const selectedExperimentChatId =
    view === "execution" && selectedExperimentRunId
      ? (project?.experiment_control[selectedExperimentRunId]?.operational?.chat_id ?? null)
      : null;
  const resolveVisibleChatTranscriptIds = useCallback(
    (selectedId: string | null, floatingId: string | null) =>
      visibleChatTranscriptIds(view, selectedId, floatingId, selectedExperimentChatId),
    [selectedExperimentChatId, view],
  );
  const {
    snapshot: chatStateSnapshot,
    chatSummariesLoading,
    visibleChatSummaries,
    selectChat,
    setFloatingChat,
    reconcileFloatingChat,
    startConversation,
    ensureConversation,
    refreshChatSummaries,
    loadMoreChatSummaries,
    recordTaskUpdates,
    recordWatcherResults,
    markVisibleChatRead,
    resetProjectChats,
    restoreProjectChats,
  } = useChatState({
    projectId,
    apiBase,
    selectedExperimentChatId,
    isActiveProject,
    visibleTranscriptIds: resolveVisibleChatTranscriptIds,
    reportError: reportErrorNotice,
  });
  const {
    floatingChat,
    draftConversations,
    selectedChatId,
    unreadChatTaskIds,
    chatSummaryTotal,
    chatSummaryNextOffset,
    chatTranscripts,
  } = chatStateSnapshot;
  const selectedExperimentTerminalVersion = selectedExperimentChatId
    ? tasks
        .filter(
          (task) =>
            task.request.chat_id === selectedExperimentChatId &&
            Boolean(task.request.result_view) &&
            RESULT_VIEW_TERMINAL_STATUSES.has(task.status),
        )
        .map((task) => `${task.operation_id}:${task.status}:${task.updated_at}`)
        .sort()
        .join("|")
    : "";
  const {
    runDialogOpen,
    autoResearchDialogOpen,
    autoResearchStartError,
    episodeAction,
    episodeRefreshError,
    episodes,
    episodeMessages,
    liveAutoResearchEpisode,
    selectedResultViews,
    selectedResultViewsError,
    openRunDialog,
    closeRunDialog,
    openAutoResearchDialog,
    closeAutoResearchDialog,
    reportAutoResearchStartError,
    beginEpisodeAction,
    replaceEpisode,
    recordEpisodeMessage,
    refreshEpisodes,
    refreshEpisodeMessages,
    refreshResultViews: refreshSelectedResultViews,
    keepSelectedResultView: keepResultViewForSelection,
  } = useEpisodeDialogs({
    projectId,
    apiBase,
    selectedExperimentId: selectedExperimentRunId,
    selectedExperimentChatId,
    isActiveProject,
  });
  const {
    snapshot: projectHistorySnapshot,
    openProjectHistory,
    closeProjectHistory,
    resetProjectHistory,
    restoreProjectHistory,
    dismissHistoryNotices,
  } = useProjectHistory({
    projectId,
    apiBase,
    loadedProjectId: project?.id ?? null,
    revision: graph.revision,
    isActiveProject,
    reportError: reportErrorNotice,
  });
  const {
    latestRevisionSummary,
    historyRevisionSummaries,
    historySummariesRevision,
    historySummariesError,
    projectHistoryOpen,
    dismissedHistoryNoticeIds,
  } = projectHistorySnapshot;
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
        ...chatStateSnapshot,
        dagRelationFocusId,
        ...agentTasksSnapshot,
        humanDraft,
        ...projectHistorySnapshot,
        usage,
        watchers,
      }
    : null;

  const rememberProjectState = useCallback(
    (id: string | null) => {
      if (!id) return;
      const current = currentProjectStateRef.current;
      if (!current || current.project.id !== id) return;
      const selection = captureProjectSelection(id);
      cacheProjectState(id, {
        ...current,
        ...selection,
        ...cloneChatStateSnapshot(current),
        ...cloneAgentTasksSnapshot(current),
        ...cloneProjectHistorySnapshot(current),
        watchers: [...current.watchers],
      });
    },
    [cacheProjectState, captureProjectSelection],
  );

  useEffect(() => {
    if (!notice) return;
    const timeoutId = window.setTimeout(() => {
      setNotice((current) => (current === notice ? null : current));
    }, NOTICE_TIMEOUT_MS);
    return () => window.clearTimeout(timeoutId);
  }, [notice]);

  const restoreProjectTabState = useCallback(
    (id: string, state: CachedProjectTabState, requestedView?: AppView) => {
      const nextGraph = state.project.graph;
      const presented = applyHumanDraft(nextGraph, state.humanDraft);
      const discardedProposalIds = state.draftReconciliationDiscardedProposalIds ?? [];
      cacheProjectState(
        id,
        discardedProposalIds.length > 0
          ? { ...state, draftReconciliationDiscardedProposalIds: [] }
          : state,
      );
      renderedRevisionRef.current = nextGraph.revision;
      authoritativeProjectId.current = id;
      replaceProject(state.project);
      restoreProjectHeader(state.projectHeaderCollapsed);
      restoreProjectSelection(id, state.project, presented.nodes, state, requestedView);
      restoreProjectChats(state, presented.nodes);
      restoreProjectTasks(state);
      setHumanDraft(state.humanDraft);
      setSyncingDraft(false);
      restoreProjectHistory(state);
      setUsage(state.usage);
      setWatchers([...state.watchers]);
      setProjectReconciliation("authoritative");
      setLoading(false);
      if (discardedProposalIds.length > 0) {
        setNotice({ kind: "info", text: proposalChoicesClearedNotice(discardedProposalIds) });
      }
    },
    [
      cacheProjectState,
      replaceProject,
      restoreProjectChats,
      restoreProjectHeader,
      restoreProjectSelection,
    ],
  );

  const applyProjectSnapshot = useCallback(
    (nextProject: ProjectSnapshot, preserveReadiness: boolean) => {
      const decodedProject = decodeProjectSnapshot(nextProject);
      const nextGraph = decodedProject.graph;
      const authoritative = decodedProject.snapshot_freshness === "fresh";
      if (
        !cachedSnapshotCanReplace(getActiveProjectId(), renderedRevisionRef.current, decodedProject)
      )
        return;
      renderedRevisionRef.current = nextGraph.revision;
      updateProject((current) =>
        preserveReadiness ? preserveProjectReadiness(decodedProject, current) : decodedProject,
      );
      applyCanonicalProject(decodedProject, authoritative);
      setHumanDraft((current) => {
        if (!current) return null;
        const reconciliation = authoritative
          ? reconcileHumanDraft(current, nextGraph)
          : { draft: normalizeHumanDraft(current, nextGraph), discardedProposalIds: [] };
        const rebased = reconciliation.draft;
        const retained = humanDraftChangeCount(rebased) > 0 ? rebased : null;
        try {
          persistProjectHumanDraft(localStorage, decodedProject.id, retained);
        } catch {
          // The in-memory draft remains usable if browser storage is unavailable.
        }
        if (reconciliation.discardedProposalIds.length > 0) {
          setNotice({
            kind: "info",
            text: proposalChoicesClearedNotice(reconciliation.discardedProposalIds),
          });
        }
        return retained;
      });
      reconcileFloatingChat(nextGraph.nodes, !authoritative);
    },
    [applyCanonicalProject, getActiveProjectId, updateProject],
  );

  const reload = useCallback(
    async (includeTasks = true) => {
      if (!projectId) return;
      const requestedProjectId = projectId;
      const base = `/api/projects/${encodeURIComponent(requestedProjectId)}`;
      const projectRequest = api<ProjectSnapshot>(base).then((nextProject) => {
        if (!isActiveProject(requestedProjectId)) return;
        applyProjectSnapshot(nextProject, authoritativeProjectId.current === requestedProjectId);
        authoritativeProjectId.current = requestedProjectId;
        setProjectReconciliation("authoritative");
      });
      const tasksRequest = includeTasks
        ? api<AgentTask[]>(`${base}/tasks`).then((nextTasks) => {
            if (isActiveProject(requestedProjectId)) replaceTasks(nextTasks);
          })
        : Promise.resolve();
      const usageRequest = api<AgentUsageSnapshot>(`${base}/usage`)
        .then((nextUsage) => {
          if (isActiveProject(requestedProjectId)) setUsage(nextUsage);
        })
        .catch((error) => {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
          if (isActiveProject(requestedProjectId)) setUsage(null);
        });
      const watchersRequest = api<WatcherRecord[]>(`${base}/watchers`).then((nextWatchers) => {
        if (isActiveProject(requestedProjectId)) setWatchers(nextWatchers);
      });
      const chatsRequest = refreshChatSummaries(requestedProjectId, base).catch((error) => {
        if (isActiveProject(requestedProjectId)) {
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
    [applyProjectSnapshot, isActiveProject, projectId, refreshChatSummaries],
  );
  reloadRef.current = reload;

  const reloadAuthoritativeProject = useCallback(
    (requestedProjectId?: string | null) => {
      const activeId = requestedProjectId ?? getActiveProjectId();
      if (!activeId || !isActiveProject(activeId)) return Promise.resolve();
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
    },
    [getActiveProjectId, isActiveProject],
  );

  const heartbeatProjectCache = useCallback(
    (requestedProjectId: string): Promise<void> =>
      runProjectHeartbeat(requestedProjectId, async () => {
        const base = `/api/projects/${encodeURIComponent(requestedProjectId)}`;
        let observedRevision: number;
        try {
          observedRevision = await loadCanonicalRevision(api, base);
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 404)) throw error;
          // A 404 here is ambiguous: the display cache may merely be missing,
          // or the project may have stopped being readable — deleted, or no
          // longer ours. The filtered index answers which.
          if (await projectIsStillReadable(api, requestedProjectId)) return;
          // Close the tab *and* leave the view: closing alone would strand the
          // reader on a project they can no longer load anything from.
          closeProjectRoute(requestedProjectId);
          removeProject(requestedProjectId);
          forgetProjectViewport(requestedProjectId);
          setNotice({ kind: "error", text: "This project is no longer available." });
          return;
        }
        const tabIsOpen = () => isProjectTabOpen(requestedProjectId);
        if (!tabIsOpen()) return;
        if (isActiveProject(requestedProjectId)) {
          if (canonicalRevisionNeedsReload(observedRevision, renderedRevisionRef.current)) {
            await reloadAuthoritativeProject(requestedProjectId);
          }
          return;
        }

        const retained = inactiveCachedProjectState(requestedProjectId);
        if (!retained || observedRevision <= retained.project.graph.revision) return;
        const snapshot = await api<ProjectSnapshot>(`${base}/cached`);
        const current = inactiveCachedProjectState(requestedProjectId);
        if (!current) {
          if (!tabIsOpen()) return;
          if (
            isActiveProject(requestedProjectId) &&
            canonicalRevisionNeedsReload(snapshot.graph.revision, renderedRevisionRef.current)
          ) {
            await reloadAuthoritativeProject(requestedProjectId);
          }
          return;
        }
        const next = reconcileInactiveProjectTabState(current, snapshot);
        if (next === current) return;
        cacheProjectState(requestedProjectId, next);
        try {
          persistProjectHumanDraft(localStorage, requestedProjectId, next.humanDraft);
        } catch {
          // A background cache refresh must not discard the in-memory draft.
        }
      }),
    [
      cacheProjectState,
      closeProjectRoute,
      forgetProjectViewport,
      inactiveCachedProjectState,
      isActiveProject,
      isProjectTabOpen,
      reloadAuthoritativeProject,
      removeProject,
      runProjectHeartbeat,
    ],
  );

  useEffect(() => {
    if (!identityReady || identityIssue || !actorIdentityChecked || teamSessionRequired) return;
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
      () => projectIdsForHeartbeat().forEach(runHeartbeat),
      () => {
        const activeId = getActiveProjectId();
        if (activeId) runHeartbeat(activeId);
      },
    );
  }, [
    actorIdentityChecked,
    heartbeatProjectCache,
    identityIssue,
    identityReady,
    getActiveProjectId,
    projectIdsForHeartbeat,
    teamSessionRequired,
  ]);

  const authenticateTeamSession = useCallback(
    async (token: string) => {
      await authenticateIdentityTeamSession(token);
      clearProjectRoute();
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    },
    [authenticateIdentityTeamSession, clearProjectRoute],
  );

  useEffect(() => {
    if (!desktop) return;
    let stopped = false;
    const cleanups: Array<() => void> = [];
    const prepareShow = async () => {
      try {
        const identity = await reverifyIdentity("prepare-show");
        if (identity.ok) {
          const activeId = getActiveProjectId();
          if (activeId) {
            const visibleProjectId = activeId;
            const nextTasks = await api<AgentTask[]>(
              `/api/projects/${encodeURIComponent(visibleProjectId)}/tasks`,
            );
            if (isActiveProject(visibleProjectId)) replaceTasks(nextTasks);
            setProjectReconciliation("reconciling");
            void reloadRef.current(false).catch((error) => {
              if (!isActiveProject(visibleProjectId) || stopped) return;
              setProjectReconciliation("failed");
              setNotice({
                kind: "error",
                text: error instanceof Error ? error.message : String(error),
              });
            });
          } else {
            const nextProjects = await api<ProjectCard[]>("/api/projects");
            if (!stopped) replaceProjects(nextProjects);
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
        if (!stopped && payload.message) reportIdentityIssue(payload.message);
        await reverifyIdentity("desktop-backend-mismatch");
      }),
      listenDesktopEvent<{ version?: string }>("rcp://update-ready", (payload) => {
        if (stopped) return;
        recordDesktopUpdateReady(payload.version, currentActiveAgentTasks());
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
      updateProject((current) => (current ? { ...current, ...readiness } : current));
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
  }, [apiBase, updateProject]);

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
          if (!isActiveProject(requestedProjectId)) return;
          updateProject((current) =>
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
              if (!isActiveProject(requestedProjectId)) return;
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
          if (!isActiveProject(requestedProjectId)) return;
          setNotice({
            kind: "error",
            text: error instanceof Error ? error.message : String(error),
          });
        });
    };
    readCachedReadiness(0);
  }, [apiBase, isActiveProject, project?.id, projectId, projectReconciliation, updateProject]);

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
      if (projectId && isActiveProject(projectId)) setUsage(nextUsage);
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
  }, [apiBase, isActiveProject, projectId]);

  const updatePaper = useCallback(
    (nextPaper: PaperSnapshot) => {
      replacePaper(nextPaper);
      updateProject((current) => (current ? { ...current, paper: nextPaper } : current));
    },
    [replacePaper, updateProject],
  );

  useEffect(() => {
    const handleHashChange = () => {
      const route = parseProjectHash(window.location.hash);
      const activeId = getActiveProjectId();
      if (route.projectId !== activeId) {
        rememberProjectState(activeId);
      }
      applyHashRoute(route.projectId, isSetupRoute());
      applyRouteSelection(route.view, route.experimentId);
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, [applyHashRoute, applyRouteSelection, getActiveProjectId, rememberProjectState]);

  useLayoutEffect(() => {
    if (!identityReady || identityIssue || !actorIdentityChecked || teamSessionRequired) return;
    const requestedRoute = parseProjectHash(window.location.hash);
    const routeMatchesProject = requestedRoute.projectId === projectId;
    const retainedOpen = projectId ? cachedProjectStateForOpen(projectId) : null;
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
      replaceProject(null);
      resetProjectSelection(
        routeMatchesProject ? requestedRoute.view : "overview",
        routeMatchesProject ? requestedRoute.experimentId : null,
      );
      resetProjectChats();
      resetProjectTasks(projectId);
      resetProjectHistory(projectId);
      setUsage(null);
      setWatchers([]);
      resetProjectHeader(projectId);
      setHumanDraft(null);
      setSyncingDraft(false);
    }
    if (setupOpen) {
      setLoading(false);
      return;
    }
    if (!projectId) {
      void refreshProjectInvitations();
      loadProjectIndex()
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
        if (cancelled || !isActiveProject(projectId)) return;
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
        if (cancelled || !isActiveProject(projectId)) return;
        if (!retained && authoritativeProjectId.current !== projectId) {
          setProjectReconciliation("failed");
        }
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      } finally {
        if (!cancelled && isActiveProject(projectId)) setLoading(false);
      }
    };
    void openProject();
    return () => {
      cancelled = true;
    };
  }, [
    applyProjectSnapshot,
    actorIdentityChecked,
    cachedProjectStateForOpen,
    identityIssue,
    identityReady,
    isActiveProject,
    loadProjectIndex,
    projectId,
    reload,
    replaceProject,
    resetProjectHeader,
    resetProjectSelection,
    restoreProjectTabState,
    selectChat,
    setupOpen,
    teamSessionRequired,
  ]);

  useEffect(() => {
    if (projectReconciliation === "authoritative") ensureProjectReadiness();
  }, [ensureProjectReadiness, projectReconciliation]);

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
  const openNodeById = (nodeId: string) => openNode(presentedGraph.nodes[nodeId] ?? null);
  const openRelatedNode = (sourceSlot: DetailWindowSlot, nodeId: string) => {
    openRelatedGraphNode(sourceSlot, presentedGraph.nodes[nodeId] ?? null);
  };
  const restoreDockedNode = (nodeId: string) => {
    restoreDockedGraphNode(nodeId, presentedGraph.nodes[nodeId] ?? null);
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
  const refreshResultViews = refreshSelectedResultViews;
  useEffect(() => {
    void refreshResultViews();
  }, [refreshResultViews, selectedExperimentTerminalVersion]);
  useEffect(() => {
    if (selectedExperimentChatId && floatingChat?.chatId === selectedExperimentChatId) {
      setFloatingChat(null);
    }
  }, [floatingChat?.chatId, selectedExperimentChatId]);
  const keepSelectedResultView = keepResultViewForSelection;
  const draftChangeCount = humanDraftChangeCount(humanDraft);
  const committableDraftCount = humanDraftCommittableCount(humanDraft, graph);
  const behindDraftCount = humanDraftBehindCount(humanDraft, graph);
  const ontologyDraftIsStale = humanDraftOntologyIsStale(humanDraft, graph);
  const chatsIndicator = chatIndicator(tasks, unreadChatTaskIds);

  const changeAppTextScale = (action: TextScaleAction) => {
    setTextScale((current) => changeTextScale(current, action));
  };

  const openChats = (preferredChatId?: string | null) => {
    const nextChatId =
      preferredChatId ??
      chatEntryConversationId(conversations, activityTask, unreadChatTaskIds, selectedChatId);
    selectChat(nextChatId);
    setFloatingChat(null);
    clearNodeSelections();
    changeView("chats");
  };

  useEffect(() => {
    if (mutationsDisabled) {
      closeRunDialog();
      closeAutoResearchDialog();
    }
  }, [mutationsDisabled]);

  useEffect(() => {
    const visibleChatId = visibleUnreadChatId(view, selectedChatId, selectedExperimentChatId);
    if (recordTaskUpdates(tasks, visibleChatId)) {
      if (projectId) {
        void refreshChatSummaries(projectId, apiBase).catch((error) => {
          setNotice({
            kind: "error",
            text: `Chats could not be refreshed: ${error instanceof Error ? error.message : String(error)}`,
          });
        });
      }
    }
  }, [
    apiBase,
    projectId,
    refreshChatSummaries,
    selectedChatId,
    selectedExperimentChatId,
    tasks,
    view,
  ]);

  useEffect(() => {
    const visibleChatId = visibleUnreadChatId(view, selectedChatId, selectedExperimentChatId);
    markVisibleChatRead(tasks, visibleChatId);
  }, [selectedChatId, selectedExperimentChatId, tasks, view]);

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
          void reverifyIdentity("active-task-poll-failure");
          consecutiveFailures += 1;
          schedule(Math.min(8000, 1000 * 2 ** (consecutiveFailures - 1)));
        }
        return;
      }
      if (stopped) return;
      const recoveredAfterFailure = consecutiveFailures > 0;
      consecutiveFailures = 0;
      if (recoveredAfterFailure) void reverifyIdentity("active-task-poll-recovered");
      const current = next.find((task) => task.operation_id === activeTask.operation_id);
      if (current && !isActiveTask(current)) {
        void api<AgentUsageSnapshot>(`/api/projects/${encodeURIComponent(projectId)}/usage`).then(
          (nextUsage) => {
            if (!stopped && isActiveProject(projectId)) setUsage(nextUsage);
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
        if (!stopped) replaceTasks(next);
        return;
      }
      replaceTasks(next);
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
        if (!stopped && isActiveProject(requestedProjectId)) {
          const hasUnseenWatcherResults = recordWatcherResults(nextTasks);
          applyProjectSnapshot(nextProject, authoritativeProjectId.current === requestedProjectId);
          authoritativeProjectId.current = requestedProjectId;
          setProjectReconciliation("authoritative");
          setWatchers(nextWatchers);
          replaceTasks(nextTasks);
          if (hasUnseenWatcherResults) {
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
    const request = toHumanSyncRequest(normalized, graph);
    setSyncingDraft(true);
    setNotice(null);
    try {
      const nextGraph = decodeGraphState(
        await api<GraphState>(`${apiBase}/sync`, {
          method: "POST",
          body: JSON.stringify(request),
        }),
      );
      const retained = retainBehindDraftAfterSync(normalized, graph, nextGraph);
      setHumanDraft(retained);
      try {
        persistProjectHumanDraft(localStorage, projectId, retained);
      } catch (error) {
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      }
      applySyncedGraph(nextGraph);
      updateProject((current) => (current ? projectWithGraph(current, nextGraph) : current));
      reconcileFloatingChat(nextGraph.nodes, false);
      await reload();
      setNotice({
        kind: "info",
        text: humanSyncSuccessNotice(nextGraph.revision, request.proposals, nextGraph),
      });
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

  const startAgentTask = async (
    kind: AgentTaskKind,
    request: AgentTaskRequest,
  ): Promise<AgentTask> => {
    const finishTaskStart = beginTaskStart();
    if (!finishTaskStart) throw new Error("Another task start is already being submitted.");
    try {
      const task = await api<AgentTask>(`${apiBase}/tasks/${kind}`, {
        method: "POST",
        body: JSON.stringify(request),
      });
      recordStartedTask(task);
      setNotice(null);
      return task;
    } finally {
      finishTaskStart();
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
    const finishExperimentStop = beginExperimentStop(nodeId);
    try {
      const control = await api<ExperimentControlState>(
        `${apiBase}/experiments/${encodeURIComponent(nodeId)}/stop`,
        { method: "POST" },
      );
      updateProject((current) =>
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
      finishExperimentStop();
    }
  };

  const checkExperimentWatcher = async (watcherId: string) => {
    if (
      !apiBase ||
      watcherCheckId ||
      taskStarting ||
      taskActionId ||
      experimentStopId ||
      mutationsDisabled
    )
      return;
    const finishWatcherCheck = beginWatcherCheck(watcherId);
    try {
      const checked = await api<WatcherRecord>(
        `${apiBase}/watchers/${encodeURIComponent(watcherId)}/check`,
        { method: "POST" },
      );
      setWatchers((current) =>
        current.map((watcher) => (watcher.watcher_id === checked.watcher_id ? checked : watcher)),
      );
      await reload();
    } catch (error) {
      setNotice({ kind: "error", text: (error as Error).message });
    } finally {
      finishWatcherCheck();
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
    const finishTaskStart = beginTaskStart();
    if (!finishTaskStart) {
      setNotice({ kind: "error", text: "Another task start is already being submitted." });
      return;
    }
    try {
      const chatId = ensureConversation(conversations, "node_chat", node, project.name);
      const profile = project.agent_profiles.node_chat;
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
      setNotice(null);
      setFloatingChat(null);
      showExperiment(node.id);
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
      finishTaskStart();
    }
  };

  const runAgent = async (config: AgentRunConfig, scope: string[], message: string | null) => {
    if (!project || taskStarting || mutationsDisabled) return;
    const runKind = project.last_refresh_at ? "refresh" : "seed";
    replaceRunScope(scope);
    try {
      await startAgentTask(runKind, {
        ...config,
        model: config.model || null,
        run_truth_scope: scope,
        message,
      });
      closeRunDialog();
    } catch (caught) {
      setNotice({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    }
  };

  const authorizeAutoResearch = async (
    invocationCeiling: number,
    startingInstruction: string | null,
  ) => {
    if (!project || !apiBase || mutationsDisabled || episodeAction || taskStarting) return;
    if (liveAutoResearchEpisode) {
      reportAutoResearchStartError("An auto-research episode is already live for this project.");
      return;
    }
    const finishTaskStart = beginTaskStart();
    if (!finishTaskStart) {
      reportAutoResearchStartError("Another task start is already being submitted.");
      return;
    }
    const finishEpisodeAction = beginEpisodeAction("start");
    if (!finishEpisodeAction) {
      finishTaskStart();
      return;
    }
    reportAutoResearchStartError(null);
    try {
      const started = await startEpisode(apiBase, {
        mode: "auto_research",
        invocation_ceiling: invocationCeiling,
        starting_instruction: startingInstruction,
      });
      replaceEpisode(started);
      closeAutoResearchDialog();
      changeView("execution");
      try {
        await reload();
      } catch (error) {
        setNotice({
          kind: "error",
          text: `Auto-research started, but Runs could not refresh: ${error instanceof Error ? error.message : String(error)}`,
        });
      }
    } catch (error) {
      reportAutoResearchStartError(error instanceof Error ? error.message : String(error));
    } finally {
      finishTaskStart();
      finishEpisodeAction();
    }
  };

  const requestEpisodeStop = async (episodeId: string) => {
    if (!apiBase || episodeAction) return;
    const finishEpisodeAction = beginEpisodeAction(`stop:${episodeId}`);
    if (!finishEpisodeAction) return;
    try {
      replaceEpisode(await stopEpisode(apiBase, episodeId));
      await refreshEpisodes();
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      throw error;
    } finally {
      finishEpisodeAction();
    }
  };

  const requestEpisodeReauthorization = async (episodeId: string, invocationCeiling: number) => {
    if (!apiBase || episodeAction) return;
    const finishEpisodeAction = beginEpisodeAction(`reauthorize:${episodeId}`);
    if (!finishEpisodeAction) return;
    try {
      const nextEpisode = await reauthorizeEpisode(apiBase, episodeId, invocationCeiling);
      replaceEpisode(nextEpisode);
      await reload();
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      throw error;
    } finally {
      finishEpisodeAction();
    }
  };

  const messageEpisodeOrchestrator = async (episodeId: string, body: string) => {
    if (!apiBase || !projectId || episodeAction) return;
    const finishEpisodeAction = beginEpisodeAction(`message:${episodeId}`);
    if (!finishEpisodeAction) return;
    try {
      const saved = await sendEpisodeMessage(apiBase, episodeId, body);
      recordEpisodeMessage(projectId, episodeId, saved);
      await refreshEpisodeMessages(episodeId);
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      throw error;
    } finally {
      finishEpisodeAction();
    }
  };

  const operateTask = async (
    task: AgentTask,
    action: "pause" | "resume" | "retry",
    presentTask = true,
  ) => {
    if (taskActionId) return;
    if (action !== "pause" && mutationsDisabled && taskMayMutateGraph(task)) return;
    const finishTaskAction = beginTaskAction(task.operation_id);
    if (!finishTaskAction) return;
    try {
      const next = await api<AgentTask>(`${apiBase}/tasks/${task.operation_id}/${action}`, {
        method: "POST",
      });
      upsertTask(next);
      if (presentTask) presentAgentTask(next);
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
      finishTaskAction();
    }
  };

  const operateEpisodeOrchestratorTask = async (
    task: AgentTask,
    action: "pause" | "resume" | "retry",
  ) => {
    await operateTask(task, action, false);
    await refreshEpisodes();
  };

  const repairGraphUpdate = async (operationId: string): Promise<void> => {
    const finishTaskRepair = beginTaskRepair(operationId);
    if (!finishTaskRepair) {
      throw new Error("Another task action is already being submitted.");
    }
    try {
      if (mutationsDisabled) {
        throw new Error("Graph repair is unavailable while replay is degraded.");
      }
      const next = await api<AgentTask>(
        `${apiBase}/tasks/${encodeURIComponent(operationId)}/repair-graph-update`,
        { method: "POST" },
      );
      recordStartedTask(next);
      setNotice(null);
    } finally {
      finishTaskRepair();
    }
  };

  const retryAgentTask = async (task: AgentTask, config: AgentRunConfig) => {
    if (taskActionId || mutationsDisabled) return;
    const finishTaskAction = beginTaskAction(task.operation_id);
    if (!finishTaskAction) return;
    try {
      const next = await api<AgentTask>(`${apiBase}/tasks/${task.operation_id}/retry`, {
        method: "POST",
        body: JSON.stringify(taskRetryRequestBody(task, config)),
      });
      upsertTask(next);
      if (!isExperimentLoopRecovery(task)) presentAgentTask(next);
      closeRetryTask();
      setNotice(null);
    } catch (caught) {
      setNotice({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      finishTaskAction();
    }
  };

  const requestRetry = (task: AgentTask) => {
    if (task.kind === "seed" || task.kind === "refresh") {
      chooseRetryTask(task);
      return;
    }
    void operateTask(task, "retry");
  };

  const commitProjectOpen = (id: string, experimentId: string | null = null) => {
    if (projectId !== id) rememberProjectState(projectId);
    commitProjectRoute(id, experimentId);
  };
  const openProject = (id: string, experimentId: string | null = null) => {
    if (requestDesktopProjectOpen(id, experimentId)) return;
    commitProjectOpen(id, experimentId);
  };
  const continueDesktopProjectOpen = () => {
    continueDesktopProjectAccess(commitProjectOpen);
  };
  const returnToProjects = () => {
    rememberProjectState(projectId);
    returnToProjectIndex();
  };

  const answerProjectInvitation = async (invitationId: string, response: "accept" | "decline") => {
    // A team space refuses a bodyless mutation: JSON-only is what stops a
    // cross-site form forging one, so even an empty body must be JSON.
    await api(`/api/project-invitations/${encodeURIComponent(invitationId)}/${response}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await refreshProjectInvitations();
    await loadProjectIndex();
  };

  const deleteProject = async (id: string) => {
    await api(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
    removeProject(id);
    forgetProjectViewport(id);
    try {
      localStorage.removeItem(humanDraftStorageKey(id));
    } catch {
      // The project is already deleted; a stranded draft key must not fail the action.
    }
  };

  const activateProjectTab = (id: string) => {
    if (id === projectId) return;
    rememberProjectState(projectId);
    activateProjectRoute(id);
  };

  const closeDockedProject = (id: string) => {
    if (!closeProjectRoute(id)) return;
    forgetProjectViewport(id);
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
      const nextProjectId = adjacentProjectId(action === "previous" ? -1 : 1);
      if (!nextProjectId) return;
      event.preventDefault();
      activateProjectTab(nextProjectId);
    };
    window.addEventListener("keydown", onProjectTabKeyDown);
    return () => window.removeEventListener("keydown", onProjectTabKeyDown);
  });

  const reconnectBackend = async () => {
    await reconnectDesktopBackend(reportIdentityIssue);
  };

  const updateHasActiveWork =
    Boolean(activeTask) ||
    (desktopUpdate?.active_agent_tasks ?? verifiedHealth?.active_agent_tasks ?? 0) > 0;
  const applyUpdate = async () => {
    await applyDesktopShellUpdate(Boolean(activeTask), async () => {
      const identity = await reverifyIdentity("update-apply");
      return {
        ok: identity.ok,
        activeAgentTasks: identity.health?.active_agent_tasks ?? 0,
      };
    });
  };

  const updateSurface =
    desktop && (desktopUpdate || updateError) ? (
      <DesktopUpdateNotice
        update={desktopUpdate}
        activeWork={updateHasActiveWork}
        expanded={updateExpanded}
        applying={updateApplying}
        error={updateError}
        onExpand={expandUpdate}
        onApply={() => void applyUpdate()}
        onDismiss={dismissUpdate}
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
          <button className="button secondary" type="button" onClick={dismissDesktopProjectOpen}>
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
              onChange={(event) => updateActorNameDraft(event.target.value)}
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
  if (!actorIdentityChecked)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>Verifying your identity</span>
        {acceptanceAgentSurface}
      </div>
    );
  if (teamSessionRequired)
    return (
      <>
        <TeamLoginBoundary
          spaceName={verifiedHealth?.space_name ?? null}
          onAuthenticate={authenticateTeamSession}
        />
        {acceptanceAgentSurface}
      </>
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
          invitations={projectInvitations}
          onAnswerInvitation={answerProjectInvitation}
          experimentLoops={experimentLoops}
          onOpen={openProject}
          onOpenExperiment={openProject}
          onCreate={openSetup}
          onDelete={deleteProject}
          openProjectTabs={openProjectTabs}
          onActivateProjectTab={activateProjectTab}
          onCloseProjectTab={closeDockedProject}
          identity={actorIdentity}
          identityError={actorIdentityError}
          onRequestIdentityName={requestActorName}
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
  const selectedExperimentNode = selectedExperimentRunId
    ? (presentedGraph.nodes[selectedExperimentRunId] ?? null)
    : null;
  const selectedExperimentControl = selectedExperimentRunId
    ? (project.experiment_control[selectedExperimentRunId] ?? null)
    : null;
  const selectedExperimentConversation =
    selectedExperimentChatId && selectedExperimentNode?.type === "experiment" ? (
      <Suspense
        fallback={
          <div className="project-view-loading" aria-label="Loading run conversation">
            <LoaderCircle className="spin" />
          </div>
        }
      >
        <NodeChat
          key={selectedExperimentChatId}
          project={project}
          node={selectedExperimentNode}
          nodes={presentedGraph.nodes}
          glossaryIndex={glossaryIndex}
          runScope={selectedExperimentControl?.operational?.session?.run_truth_scope ?? runScope}
          tasks={tasks}
          watchers={watchers}
          historyMessages={chatTranscripts.get(selectedExperimentChatId)?.messages}
          chatId={selectedExperimentChatId}
          presentation="workspace"
          fixedConversation
          graphChangesDisabled={mutationsDisabled}
          resultViews={selectedResultViews}
          resultViewsError={selectedResultViewsError}
          onKeepResultView={keepSelectedResultView}
          onStartTask={startAgentTask}
          onResumeTask={(task) => void operateTask(task, "resume")}
          onRetryTask={requestRetry}
          onInspectTask={selectTaskInspector}
          onOpenInbox={() => changeView("attention")}
          onRepairGraphUpdate={repairGraphUpdate}
          onOpenNode={openNodeById}
          onStopWatcher={(watcherId) => void stopWatcher(watcherId)}
          onNewSession={() => undefined}
          onClose={() => undefined}
        />
      </Suspense>
    ) : undefined;

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
                  const chatId = startConversation("project_chat", null, project.name);
                  openChats(chatId);
                }}
              >
                <MessageCircle size={14} /> Ask
              </button>
              <button
                className="button secondary auto-research-control"
                disabled={
                  mutationsDisabled ||
                  projectReconciliation !== "authoritative" ||
                  !project.canonical_state.reachable ||
                  taskStarting ||
                  Boolean(liveAutoResearchEpisode)
                }
                aria-label="Auto-research"
                title={
                  liveAutoResearchEpisode ? "An auto-research episode is already live." : undefined
                }
                onClick={() => {
                  openAutoResearchDialog();
                }}
              >
                <Telescope size={14} /> <span className="auto-research-label">Auto-research</span>
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
                onClick={openProjectHistory}
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
                onClick={openRunDialog}
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
          onClick={toggleProjectHeader}
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
                : item.view === "scientific"
                  ? openLastResearchView()
                  : changeView(item.view)
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
              onChange={(event) => changeTrustView(event.target.value as TrustView)}
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
        {episodeRefreshError && (
          <div className="coverage-banner replay-degraded" role="alert">
            <AlertTriangle size={15} />
            <span>{episodeRefreshError}</span>
          </div>
        )}
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
                  graph={graph}
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
              onClearRelationFocus={clearDagRelationFocus}
              onSelectNode={openNode}
            />
          )}
          {view === "execution" && (
            <div className="combined-runs-view">
              <AutoResearchEpisodes
                episodes={episodes}
                tasks={tasks}
                messagesByEpisode={episodeMessages}
                busyAction={episodeAction}
                taskActionId={taskActionId}
                onInspectTask={selectTaskInspector}
                onLoadMessages={refreshEpisodeMessages}
                onStop={requestEpisodeStop}
                onReauthorize={requestEpisodeReauthorization}
                onSendMessage={messageEpisodeOrchestrator}
                onOperateTask={operateEpisodeOrchestratorTask}
              />
              <ExecutionView
                graph={presentedGraph}
                attentionBlockerIds={attentionBlockerIds}
                tasks={tasks.filter((task) => task.kind !== "auto_research")}
                watchers={watchers}
                experimentControl={project.experiment_control}
                dismissedTaskIds={dismissedTaskIds}
                selectedExperimentId={selectedExperimentRunId}
                focusExperimentId={focusExperimentRunId}
                runBusy={taskStarting}
                stopBusyId={experimentStopId}
                watcherCheckBusyId={watcherCheckId}
                taskActionId={taskActionId}
                selectedExperimentConversation={selectedExperimentConversation}
                providerLabels={Object.fromEntries(
                  Object.entries(project.providers).map(([id, provider]) => [
                    id,
                    provider.label || id,
                  ]),
                )}
                mutationsDisabled={mutationsDisabled}
                onInspectTask={selectTaskInspector}
                onDismissTask={dismissTaskNotification}
                onSelectNode={openNode}
                onSelectExperiment={selectExperiment}
                onDetailFocused={clearExperimentFocus}
                onRunExperiment={(node) => void runExperiment(node)}
                onStopExperiment={(nodeId) => void stopExperimentLoop(nodeId)}
                onCheckExperimentWatcher={(watcherId) => void checkExperimentWatcher(watcherId)}
                onRecoverExperiment={(task, action) => void operateTask(task, action, false)}
                onSwitchExperimentProvider={chooseRetryTask}
                episodeReportHref={(episodeId) => episodeReportPreviewUrl(project.id, episodeId)}
              />
            </div>
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
              identity={actorIdentity}
              onLeftProject={() => {
                closeProjectRoute(project.id);
                removeProject(project.id);
                forgetProjectViewport(project.id);
                setNotice({ kind: "info", text: "You left this project." });
              }}
              usage={usage}
              onRefreshUsage={refreshUsage}
              cacheClearDisabled={Boolean(activeTask)}
              writesDisabled={mutationsDisabled}
              showDisplaySettings={desktop}
              textScale={textScale}
              onTextScaleChange={changeAppTextScale}
              onRefreshReadiness={refreshReadiness}
              onCacheMetricsChange={(cacheMetrics) => {
                updateProject((current) =>
                  current ? { ...current, cache_metrics: cacheMetrics } : current,
                );
              }}
              onSaved={(saved, preserveReadiness = true) => {
                const decoded = decodeProjectSnapshot(saved);
                updateProject((current) =>
                  preserveReadiness ? preserveProjectReadiness(decoded, current) : decoded,
                );
                replaceRunScope(decoded.default_run_truth_scope);
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
              onInspectTask={selectTaskInspector}
              onOpenInbox={() => changeView("attention")}
              onRepairGraphUpdate={repairGraphUpdate}
              onStopWatcher={(watcherId) => void stopWatcher(watcherId)}
              onNewSession={(conversation) => {
                const node = conversation.nodeId
                  ? (presentedGraph.nodes[conversation.nodeId] ?? null)
                  : null;
                selectChat(startConversation(conversation.kind, node, project.name));
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
              const chatId = ensureConversation(conversations, "node_chat", node, project.name);
              selectChat(chatId);
              setFloatingChat({ chatId, nodeId: node.id });
            }}
            onOpenRelatedNode={(nodeId) => openRelatedNode(slot, nodeId)}
            onSelectNode={openNodeById}
          />
        );
      })}
      {floatingChat && floatingChat.chatId !== selectedExperimentChatId && (
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
              onInspectTask={selectTaskInspector}
              onOpenInbox={() => {
                setFloatingChat(null);
                changeView("attention");
              }}
              onRepairGraphUpdate={repairGraphUpdate}
              onOpenNode={openNodeById}
              onStopWatcher={(watcherId) => void stopWatcher(watcherId)}
              onNewSession={() => {
                const node = presentedGraph.nodes[floatingChat.nodeId] ?? null;
                const chatId = startConversation("node_chat", node, project.name);
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
        onClose={closeRunDialog}
        onRun={(config, scope, message) => void runAgent(config, scope, message)}
      />
      <AutoResearchDialog
        open={autoResearchDialogOpen}
        busy={episodeAction === "start"}
        error={autoResearchStartError}
        initialInvocationCeiling={project.default_auto_research_invocation_ceiling}
        onClose={closeAutoResearchDialog}
        onAuthorize={(invocationCeiling, startingInstruction) =>
          void authorizeAutoResearch(invocationCeiling, startingInstruction)
        }
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
          onClose={closeRetryTask}
          onRun={(config) => void retryAgentTask(retryTask, config)}
        />
      )}
      {projectHistoryOpen && (
        <ProjectHistoryDrawer
          projectId={project.id}
          summaries={historyRevisionSummaries}
          tasks={tasks}
          loading={historySummariesRevision !== graph.revision}
          error={historySummariesError}
          onInspectTask={(taskId) => {
            closeProjectHistory();
            selectTaskInspector(taskId);
          }}
          episodeReportHref={(episodeId) => episodeReportPreviewUrl(project.id, episodeId)}
          onClose={closeProjectHistory}
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
          onSelect={selectTaskInspector}
          onPause={() => inspectedTask && void operateTask(inspectedTask, "pause")}
          onResume={() => inspectedTask && void operateTask(inspectedTask, "resume")}
          onRetry={() => inspectedTask && requestRetry(inspectedTask)}
          onDismiss={() => inspectedTask && dismissTaskNotification(inspectedTask.operation_id)}
          onClose={() => selectTaskInspector(null)}
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

function readTextScale(): number {
  try {
    return normalizeTextScale(localStorage.getItem(TEXT_SCALE_STORAGE_KEY));
  } catch {
    return normalizeTextScale(null);
  }
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
