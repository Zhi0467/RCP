import { projectGroupHash } from "./projects/projectGroups";
import { EvidenceFlagProvider } from "./graph/EvidenceFlagContext";
import { EvidenceFlagInbox } from "./projects/EvidenceFlagInbox";
import { evidenceFlagRows } from "./projects/evidenceFlags";
import { errorMessage } from "./core/errors";
import { PinnedView, PinnedViewTab } from "./graph/PinnedView";
import { pinRequest, pinScopeKey, researchViewsProfile } from "./graph/researchViewResources";
import { useProjectTabResources } from "./projects/projectTabResources";
import { beginTaskMutation, beginTaskPublication } from "./agents/taskActions";
import { beginChatSend } from "./agents/chatSendMutation";
import { ArtifactViewer } from "./artifacts/ArtifactViewer";
import { ResourceStore, resourceKeyId } from "./core/resourceStore";
import {
  RefreshScheduler,
  WATERMARK_REQUEST_TIMEOUT_MS,
  type OpenProject,
  type WatermarkResponse,
} from "./core/refreshScheduler";
import {
  RefreshRuntimeContext,
  useRefreshRuntime,
  useRefreshHealth,
  useResource,
} from "./core/useResource";
import {
  taskResourceProfile,
  tasksResourceKey,
  taskStatusProfile,
  usageProfile,
} from "./agents/taskResources";
import {
  type SettingsResource,
  settingsProfile,
  providerLoginsProfile,
  useProjectResource,
} from "./projects/projectResources";
import {
  graphProfile,
  graphResourceKey,
  manifestProfile,
  refsProfile,
  historyProfile,
} from "./graph/graphResources";
import { chatsResourceProfile } from "./chat/chatResources";
import {
  watchersProfile,
  watcherResourceKey,
  episodesProfile,
  experimentIndexProfile,
} from "./experiments/episodeResources";
import { paperProfile } from "./paper/paperResources";
import type { PaperEditorState } from "./paper/PaperWorkspace";
import { canStartExperiment } from "./experiments/experimentStart";
import { ExperimentStartOverlap } from "./experiments/ExperimentStartOverlap";
import { UpdateNotice } from "./desktop/UpdateNotice";
import { ReconnectOverlay } from "./desktop/ReconnectOverlay";
import { useRecheckOnRelease, useUpdateNotice } from "./desktop/useUpdateNotice";
import { TerminalTab } from "./terminals/TerminalTab";
import { GraphPicker } from "./graph/GraphPicker";
import { ViewPicker, type ResearchLayout } from "./graph/ViewPicker";
import { selectionAfterGraphSwitch } from "./graph/graphPickerModel";
import {
  branchOwnerEpisode,
  experimentStartTarget,
  graphSessionKey,
  graphTargetFromHash,
  graphTargetUrl,
  graphViewHash,
  MAIN_GRAPH,
  projectViewHash,
  sameGraphTarget,
} from "./core/graphTarget";
import type { GraphEditingProps } from "./graph/canvasEditing";
import type { GraphEditOptions } from "./core/types";
import {
  TriangleAlert,
  ArrowLeft,
  CloudUpload,
  ChevronDown,
  ChevronUp,
  FileText,
  FlaskConical,
  FolderLock,
  Files,
  GitBranch,
  History,
  Inbox,
  LayoutList,
  LoaderCircle,
  MessageCircle,
  Network,
  RefreshCw,
  RotateCcw,
  Settings,
  Telescope,
  SquareTerminal,
  X,
} from "lucide-react";
import {
  lazy,
  Suspense,
  createElement,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { isActiveTask } from "./agents/agentTasks";
import { mergeProviderLogins } from "./projects/providers";
import { loadChatTranscript } from "./chat/chatApi";
import {
  bindArtifactResourceStore,
  closeArtifactViewer,
  openArtifact,
  openEpisodeReport,
  setArtifactViewerHidden,
  useDockedArtifact,
} from "./artifacts/artifactViewerModel";
import {
  chatIndicator,
  conversationHasUnread,
  listedAgentConversations,
  withEpisodeAttention,
  unreadChatIdsFromReads,
  conversationHref,
  chatEntryConversationId,
  groupChatConversations,
  startConversationTurn,
  unsentConversation,
  type ChatKind,
  type ConversationTurnSubmission,
} from "./chat/chatWorkspace";
import {
  api,
  archiveEpisode,
  ApiError,
  loadEpisodes,
  loadProjectExperimentEpisodes,
  loadTeamSessions,
  loadProjectReadiness,
  loadProviderLogins,
  loadMergePreview,
  mergeEpisodeToMain,
  continueEpisode,
  refreshProposal,
  sendEpisodeMessage,
  startEpisode,
  startExperimentRun,
  stopEpisode,
  createGraphFork,
} from "./core/api";
import {
  BACKEND_IDENTITY_EVENT,
  backendReconnectLabel,
  desktopShowReady,
  setDesktopWebviewZoom,
  isDesktopRuntime,
  listenDesktopEvent,
  openDesktopArtifactPdf,
  returnDesktopToPersonal,
  reverifyBackendIdentity,
  TEAM_TRANSPORT_RECOVERED,
  type BackendIdentityEventDetail,
} from "./core/desktopRuntime";
import {
  projectGraphMutationFailureLabel,
  projectGraphMutationsDisabled,
  taskMayMutateGraph,
} from "./graph/graphAuthority";
import { buildGlossaryIndex } from "./graph/glossary";
import {
  experimentBoardRouteToken,
  experimentIndexEntryForRoute,
  experimentStopPath,
  mainExperimentRouteMatchesControl,
  parseProjectHash,
  projectExperimentExecution,
  type ProjectHashRoute,
} from "./experiments/experimentBoardModel";
import {
  reduceProjectTransitionProjection,
  transitionSnapshotRefusal,
  transitionSyncCompletionDisposition,
  type ProjectTransitionProjection,
} from "./graph/projectTransition";
import { nodeDetailSizeStorageKey, type DetailWindowSlot } from "./ui/floatingWindow";
import { autoResearchStartRefusal, episodeReportPreviewUrl } from "./experiments/campaigns";
import { useAgentTasks } from "./agents/useAgentTasks";
import { useActorIdentity } from "./desktop/useActorIdentity";
import {
  cloneChatStateSnapshot,
  useChatState,
  visibleChatTranscriptIds,
  visibleUnreadChatId,
} from "./chat/useChatState";
import { useDesktopShell } from "./desktop/useDesktopShell";
import { useEpisodeDialogs } from "./experiments/useEpisodeDialogs";
import { emptyGraph, useGraphSelection } from "./graph/useGraphSelection";
import {
  cloneProjectHistorySnapshot,
  useProjectHistory,
  validationNoticeId,
} from "./graph/useProjectHistory";
import { useProjectTabs } from "./projects/useProjectTabs";
import {
  cachedSnapshotCanReplace,
  canonicalGraphHead,
  latestSnapshotRequestCanApply,
  persistProjectHumanDraft,
  projectDraftPreviewEffectInputs,
  projectSettingsSavedProject,
  RETAIN_ALL_PROJECT_READINESS,
  serializeProjectSessionTabState,
  trustedProjectTransitionManifest,
} from "./graph/projectSession";
import { useProjectSession } from "./graph/useProjectSession";
import { AutoResearchDialog } from "./experiments/AutoResearchDialog";
import { AgentTaskInspector } from "./agents/AgentTaskInspector";
import { AttentionRail, ProposalJudgmentSection } from "./graph/AttentionRail";
import { ConsolidationInbox } from "./projects/ConsolidationInbox";
import {
  CONSOLIDATION_SETTINGS_ANCHOR,
  consolidationAttentionCount,
  consolidationNeedsRenewal,
  openConsolidationItems,
  resolveConsolidationRun,
} from "./projects/consolidation";
import { useConsolidation } from "./projects/useConsolidation";
import { useProjectDigest } from "./projects/useProjectDigest";
import { digestChangedNodeIds } from "./projects/projectDigest";
import { DetailDrawer } from "./graph/DetailDrawer";
import { branchNameRefusal } from "./graph/BranchNameField";
import { branchName } from "./core/branchNames";
import { DraggableWindow } from "./ui/DraggableWindow";
import { ProjectHistoryDrawer } from "./agents/ProjectHistoryDrawer";
import { ProjectDock } from "./projects/ProjectDock";
import { PhoneAskButton, PhoneProjectBar, PhoneTabBar } from "./projects/PhoneProjectChrome";
import { useNarrowViewport } from "./ui/useNarrowViewport";
import { RunDialog } from "./experiments/RunDialog";
import { initialOwnerCode } from "./desktop/pairingLink";
import { ProjectLocatorBoundary } from "./projects/ProjectLocatorBoundary";
import { TeamLoginBoundary } from "./desktop/TeamLoginBoundary";
import { dropUnscopedDrafts } from "./core/draftStorage";
import {
  applyHumanDraft,
  deserializeHumanDraft,
  draftNodeIsBehind,
  humanDraftBehindCount,
  humanDraftChangeCount,
  humanDraftCommittableCount,
  humanDraftOntologyIsStale,
  humanDraftStorageKey,
  humanSyncFailure,
  normalizeHumanDraft,
  stageDecisionChoice,
  stageNodeEdit,
  stageNodeEditStart,
  stageNodeRemoval,
  stageNodeStanding,
  stageProposalDecision,
  stageCanvasNode,
  updateStagedNode,
  stageEdgeReplacement,
  stageNodesRemoval,
  stageMerge,
  stageEdgeAddition,
  stageEdgeRemoval,
  unstageCustomNode,
  unstageNodeRemoval,
  toHumanSyncRequest,
  type HumanDraft,
} from "./graph/humanDraft";
import type {
  AgentRunConfig,
  GraphRef,
  AgentTask,
  AgentTaskKind,
  AgentTaskRequest,
  AppView,
  Episode,
  ProjectArtifact,
  EpisodeIsolationChoice,
  MergeDiffPath,
  MergeEpisodeBody,
  ExperimentControlState,
  ExperimentStartResponse,
  LoopOverlap,
  GraphNode,
  GraphState,
  GraphTargetRef,
  GraphUpdateRecovery,
  Health,
  ProjectDeletionResult,
  ProjectInvitation,
  ProjectSnapshot,
  ProjectTransitionResponse,
  TransitionPreviewResponse,
  WatcherRecord,
} from "./core/types";
import { decodeProjectTransitionResponse, DISPLAY_NAME_MAX_LENGTH } from "./core/types";
import { ProjectLanding } from "./projects/ProjectLanding";
import { SpaceSettings } from "./projects/SpaceSettings";
import { LandingIdentityMenu } from "./projects/LandingIdentityMenu";
import { ProjectOverview } from "./projects/ProjectOverview";
import { isControlNode, primaryField } from "./graph/researchType";
import { ProjectSetup } from "./projects/ProjectSetup";
import {
  parseProjectSetupRoute,
  projectMoveSetupHash,
  type ProjectSetupRoute,
} from "./projects/projectSetupModel";
import {
  changeTextScale,
  normalizeTextScale,
  TEXT_SCALE_STORAGE_KEY,
  textScaleShortcut,
  type TextScaleAction,
} from "./ui/textScale";
import { useTheme } from "./ui/useTheme";
import { IDEMPOTENT_TASK_START_TIMEOUT_MS, NOTICE_TIMEOUT_MS } from "./core/uiConstants";
import {
  createWebMcpToolRegistry,
  episodeStopRefusal,
  experimentStartRefusal,
  projectArtifactToolDefinitions,
  type ProjectArtifactRecord,
  projectAutoResearchToolDefinitions,
  projectConversationSendToolDefinitions,
  projectConversationToolDefinitions,
  projectEpisodeStopToolDefinitions,
  projectExperimentToolDefinitions,
  projectIndexToolDefinitions,
  projectReadToolDefinitions,
  playbookToolDefinitions,
  useProjectBroadReadTools,
  providerLoginToolDefinitions,
  projectViewToolDefinitions,
  type WebMcpToolRegistry,
  type WebMcpViewOwners,
  webMcpSurface,
} from "./webmcp/index";
import { publishToolSurface, webMcpHostDefinitions } from "./voice/toolCatalog";
import { voiceTerminalToolDefinitions } from "./voice/voiceTerminal";
import { openTerminalSocket } from "./terminals/terminalSocket";
import { useVoiceAgent, type VoicePageState } from "./voice/useVoiceAgent";
import { VoiceButton, VoicePanel } from "./voice/VoicePanel";

import {
  initialProjectHash,
  isEditableShortcutTarget,
  projectTabShortcut,
} from "./projects/projectTabs";
import {
  episodeNotificationHash,
  graphNotificationHash,
  initialNotificationLink,
  isNotificationLinkHash,
  parseNotificationLink,
  type NotificationLink,
} from "./core/notificationLinks";
import { loadNotificationDevices, reconcileWebPush } from "./desktop/notificationDevicesModel";
import { unfinishedJobsFromError } from "./graph/mergePanel";
import {
  attentionGraphForProjection,
  decisionsAwaitingChoice,
  humanAttentionBlockers,
  projectAttentionForPresentation,
} from "./projectAttention";

import {
  type ProjectTabUiState,
  emptyProjectCounts,
  projectWithTransitionProjection,
} from "./projectSnapshot";
import {
  activeBranchMergeTask,
  isExperimentLoopRecovery,
  retryProfileKind,
  taskActionNeedsAuthoritativeProjectReload,
  taskRetryConfig,
  taskRetryRequestBody,
} from "./taskRecovery";
import {
  experimentStartNeedsSync,
  humanDraftTransitionRouting,
  humanSyncSuccessNotice,
  localDraftTransitionProjection,
  previewTraceMismatch,
  proposalChoicesClearedNotice,
  transitionProjectionForRoute,
} from "./transitionRouting";

export {
  attentionGraphForProjection,
  decisionsAwaitingChoice,
  humanAttentionBlockers,
  projectAttentionForPresentation,
} from "./projectAttention";
export { projectWithGraph } from "./projectSnapshot";
export {
  activeBranchMergeTask,
  retryProfileKind,
  taskActionNeedsAuthoritativeProjectReload,
  taskRetryRequestBody,
  terminalTaskNeedsAuthoritativeProjectReload,
} from "./taskRecovery";
export {
  experimentStartNeedsSync,
  humanDraftTransitionRouting,
  humanSyncSuccessNotice,
  proposalChoicesClearedNotice,
  transitionProjectionForRoute,
} from "./transitionRouting";

const AttentionOverview = lazy(() =>
  import("./graph/GraphViews").then((module) => ({ default: module.AttentionOverview })),
);
const DagView = lazy(() =>
  import("./graph/GraphViews").then((module) => ({ default: module.DagView })),
);
const ExecutionView = lazy(() =>
  import("./graph/GraphViews").then((module) => ({ default: module.ExecutionView })),
);
const ResearchDrilldown = lazy(() =>
  import("./graph/ResearchDrilldown").then((module) => ({ default: module.ResearchDrilldown })),
);
const LoadedPaperWorkspace = lazy(() =>
  import("./paper/PaperWorkspace").then((module) => ({ default: module.LoadedPaperWorkspace })),
);
const ProjectSettings = lazy(() =>
  import("./projects/ProjectSettings").then((module) => ({ default: module.ProjectSettings })),
);
const ChatsWorkspace = lazy(() =>
  import("./chat/ChatsWorkspace").then((module) => ({ default: module.ChatsWorkspace })),
);
const NodeChat = lazy(() =>
  import("./chat/NodeChat").then((module) => ({ default: module.NodeChat })),
);

const Artifacts = lazy(() =>
  import("./artifacts/Artifacts").then((module) => ({ default: module.Artifacts })),
);

const Terminals = lazy(() =>
  import("./terminals/Terminals").then((module) => ({ default: module.Terminals })),
);

const navItems: Array<{ view: AppView; label: string; icon: React.ReactNode }> = [
  { view: "overview", label: "Overview", icon: <LayoutList size={14} /> },
  { view: "attention", label: "Inbox", icon: <Inbox size={14} /> },
  { view: "scientific", label: "Research", icon: <GitBranch size={14} /> },
  { view: "execution", label: "Runs", icon: <FlaskConical size={14} /> },
  // Paper is a sub-panel of Artifacts; its route stays `paper`.
  { view: "artifacts", label: "Artifacts", icon: <Files size={14} /> },
  { view: "terminals", label: "Terminals", icon: <SquareTerminal size={14} /> },
  { view: "chats", label: "Agents", icon: <MessageCircle size={14} /> },
  { view: "settings", label: "Settings", icon: <Settings size={14} /> },
];

/** Every project view renders on main and on a branch; main-only rules live inside views. */
const GRAPH_REF_VIEWS: ReadonlySet<AppView> = new Set<AppView>([
  ...navItems.map((item) => item.view),
  "dag",
  "paper",
]);

/** A tab stays highlighted while one of its sub-views is open. */
function navItemActive(item: AppView, view: AppView): boolean {
  return (
    view === item ||
    (item === "scientific" && view === "dag") ||
    (item === "artifacts" && view === "paper")
  );
}

function ArtifactsSubnav({
  view,
  filesActive = true,
  paperUnsynced,
  failed = false,
  onChange,
  children,
}: {
  view: AppView;
  filesActive?: boolean;
  paperUnsynced: boolean;
  failed?: boolean;
  onChange: (view: AppView) => void;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={`artifacts-subnav${failed ? " failed" : ""}`}
      role="group"
      aria-label="Artifacts sections"
    >
      {(
        [
          ["artifacts", "Files"],
          ["paper", "Paper"],
        ] as const
      ).map(([target, label]) => (
        <button
          key={target}
          type="button"
          aria-pressed={view === target && (target !== "artifacts" || filesActive)}
          onClick={() => onChange(target)}
        >
          {target === "paper" ? <FileText size={14} /> : <Files size={14} />}
          {label}
          {target === "paper" && paperUnsynced && <small>1</small>}
        </button>
      ))}
      {children}
    </div>
  );
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

type ProjectReconciliation = "opening" | "reconciling" | "authoritative" | "failed";

export {
  cachedSnapshotCanReplace,
  canonicalGraphHead,
  latestSnapshotRequestCanApply,
  persistProjectHumanDraft,
};

export default function App() {
  const desktop = useMemo(() => isDesktopRuntime(), []);
  useState(() => {
    const navigation = window.performance.getEntriesByType("navigation")[0] as
      PerformanceNavigationTiming | undefined;
    const requestedHash = window.location.hash;
    const hash = isSetupHash(requestedHash)
      ? requestedHash
      : initialProjectHash(requestedHash, navigation?.type, desktop);
    if (hash !== window.location.hash) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    }
    return null;
  });
  const identity = useActorIdentity();
  // Retained state belongs to the member. Backend restarts reconcile the held
  // project; a different member still drops every retained snapshot atomically.
  const sessionKey = JSON.stringify([
    identity.actorIdentity?.space_id,
    identity.actorIdentity?.space_kind,
    identity.actorIdentity?.user.user_id,
    identity.actorIdentity?.user.identity_kind,
  ]);
  useLayoutEffect(() => closeArtifactViewer(), [sessionKey]);
  return <RefreshMemberApp key={sessionKey} desktop={desktop} identity={identity} />;
}

interface RefreshBindings {
  open: OpenProject[];
  active: string | null;
  lostAccess(projectId: string): void;
}
function RefreshMemberApp(props: {
  desktop: boolean;
  identity: ReturnType<typeof useActorIdentity>;
}) {
  const bindings = useRef<RefreshBindings>({ open: [], active: null, lostAccess: () => {} });
  const runtime = useMemo(() => {
    const store = new ResourceStore();
    // The epoch changes with the backend process, so a restart is seen on the
    // next beat and Reconnect is offered before a write meets the instance fence.
    let epoch: string | null = null;
    const scheduler = new RefreshScheduler({
      store,
      clientId: crypto.randomUUID(),
      openProjects: () => bindings.current.open,
      activeProjectId: () => bindings.current.active,
      onLostAccess: (id) => bindings.current.lostAccess(id),
      send: async (request, signal) => {
        const response = await api<WatermarkResponse>(
          "/api/resource-watermarks",
          {
            method: "POST",
            body: JSON.stringify(request),
            signal,
          },
          { attemptTimeoutMs: WATERMARK_REQUEST_TIMEOUT_MS, readOnly: true },
        );
        if (epoch !== null && epoch !== response.epoch)
          void reverifyBackendIdentity("watermark-epoch");
        epoch = response.epoch;
        return response;
      },
    });
    return { store, scheduler, backendKey: props.identity.backendKey };
  }, [props.identity.backendKey]);
  useEffect(
    () => () => {
      runtime.scheduler.stop();
      runtime.store.reset();
    },
    [runtime],
  );
  useLayoutEffect(() => bindArtifactResourceStore(runtime.store), [runtime]);
  return (
    <RefreshRuntimeContext.Provider value={runtime}>
      <MemberApp {...props} refreshBindings={bindings} />
      <RefreshHealthMarker />
      <ArtifactViewer />
    </RefreshRuntimeContext.Provider>
  );
}
function RefreshHealthMarker() {
  const health = useRefreshHealth();
  return health.failing ? (
    <span
      role="status"
      aria-label="Backend contact stale"
      title="Backend contact stale"
      style={{ position: "fixed", top: 8, right: 8, zIndex: 1000, color: "var(--danger, #b33)" }}
    >
      ●
    </span>
  ) : null;
}

function MemberApp({
  desktop,
  identity,
  refreshBindings,
}: {
  desktop: boolean;
  identity: ReturnType<typeof useActorIdentity>;
  refreshBindings: React.RefObject<RefreshBindings>;
}) {
  const { store, scheduler } = useRefreshRuntime();
  const refreshHealth = useRefreshHealth();
  const [initialRoute] = useState(() => ({
    project: parseProjectHash(window.location.hash),
    graphTarget: graphTargetFromHash(window.location.hash),
    setupOpen: isSetupHash(window.location.hash),
  }));
  const [graphTarget, setGraphTarget] = useState<GraphTargetRef>(initialRoute.graphTarget);
  const [groupFiling, setGroupFiling] = useState<{ projectId: string; groupId: string } | null>(
    null,
  );
  const [requestedChat, setRequestedChat] = useState(() => ({
    hash: window.location.hash,
    projectId: initialRoute.project.projectId,
    chatId: initialRoute.project.chatId,
    graphTarget: initialRoute.graphTarget,
  }));
  // The Agents tab opens the board; anything that names a chat opens that chat.
  const [agentsBoard, setAgentsBoard] = useState(!initialRoute.project.chatId);
  const activeGraphTargetRef = useRef(graphTarget);
  // One request id per logical continuation, kept until the server has answered
  // it, so a retry after a lost response replays the episode already created.
  const continuationRequestIds = useRef(new Map<string, string>());
  const taskActionRequestIds = useRef(new Map<string, string>());
  activeGraphTargetRef.current = graphTarget;
  const {
    identityReady,
    identityIssue,
    verifiedHealth,
    backendKey,
    authenticatedHealth,
    actorIdentity,
    actorIdentityError,
    actorIdentityChecked,
    teamSessionRequired,
    ownerSessionRequired,
    authenticateOwnerSession,
    actorNamePromptOpen,
    actorNameDraft,
    actorNameSaving,
    actorNameError,
    requestActorName,
    settleActorNamePrompt,
    saveActorName,
    authenticateTeamSession,
    pairTeamDevice,
    reportIdentityIssue,
    reverifyIdentity,
    currentActiveAgentTasks,
    updateActorNameDraft,
  } = identity;
  // Every backend-facing surface, including the WebMCP inventory, waits for the
  // same verified identity, actor, and team-session state that gates the page.
  const backendSessionReady =
    identityReady &&
    !identityIssue &&
    actorIdentityChecked &&
    !!actorIdentity &&
    !teamSessionRequired &&
    !ownerSessionRequired;
  const backendSessionReadyRef = useRef(backendSessionReady);
  backendSessionReadyRef.current = backendSessionReady;
  // Persisted drafts are scoped to this member; none is read or written without one.
  const draftActorId = backendSessionReady ? (actorIdentity?.user.user_id ?? null) : null;
  const draftActorIdRef = useRef(draftActorId);
  draftActorIdRef.current = draftActorId;
  useEffect(() => {
    try {
      dropUnscopedDrafts(localStorage);
      dropUnscopedDrafts(sessionStorage);
    } catch {
      // Unavailable storage holds no drafts to drop.
    }
  }, []);
  const [releaseUpdate, setReleaseUpdate] = useUpdateNotice(backendSessionReady);
  const {
    buildIdentity,
    reconnecting,
    desktopUpdate,
    updaterEnabled,
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
  useRecheckOnRelease(releaseUpdate, desktopUpdate?.version, refreshDesktopUpdate);
  const [experimentStartOverlap, setExperimentStartOverlap] = useState<{
    projectId: string;
    loops: LoopOverlap;
  } | null>(null);
  const [notice, setNotice] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const [webMcpExperimentStartProjectId, setWebMcpExperimentStartProjectId] = useState<
    string | null
  >(null);
  const showWebMcpArtifactViewer = useCallback(
    async (record: ProjectArtifactRecord, projectId: string) => {
      if (record.view === "pdf") {
        // The panel's own path: the desktop's system PDF viewer; a browser offers Download.
        if (!isDesktopRuntime() || !record.artifact_id) return false;
        await openDesktopArtifactPdf({
          projectId,
          artifactId: record.artifact_id,
          ...(record.task_id ? { taskId: record.task_id } : {}),
        });
        return true;
      }
      if (record.view === "file") return false;
      const response = await fetch(record.content_url, {
        method: "HEAD",
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(`Artifact content is unavailable (${response.status}).`);
      }
      if (record.artifact_id) openArtifact({ projectId, artifactId: record.artifact_id });
      else if (record.episode_id)
        await openEpisodeReport({ projectId, episodeId: record.episode_id });
      else return false;
      return true;
    },
    [],
  );
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
    state: projectSession,
    dispatchProjectSession,
    getProjectSessionState,
    beginProjectSnapshotRequest,
    updateProjectHumanDraft,
    undoHumanDraft,
    redoHumanDraft,
    canUndo,
    canRedo,
    beginProjectSync,
    restoreProjectSessionTab,
  } = useProjectSession(initialRoute.project.projectId);
  const {
    project: sessionProject,
    transitionHead,
    transitionRulesetTag,
    draftTransitionProjection,
    draftPreviewConflict,
    draftPreviewPending,
  } = projectSession;
  const {
    projectId,
    setupOpen,
    projects,
    projectGroups,
    openProjectTabs,
    experimentLoops,
    experimentLoopsLoaded,
    experimentLoopsUnavailable,
    experimentLoopsUnavailableRows,
    spaceRuns,
    projectHeaderCollapsed,
    isActiveProject,
    getActiveProjectId,
    loadProjectIndex,
    refreshExperimentLoops,
    refreshProjectExperimentLoops,
    refreshSpaceRuns,
    replaceEpisodeRunArchive,
    applyHashRoute,
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
    clearProjectCache,
    rememberLeftRoute,
    cachedProjectStateForOpen,
    adjacentProjectId,
  } = useProjectTabs<ProjectTabUiState>({
    initialProjectId: initialRoute.project.projectId,
    initialSetupOpen: initialRoute.setupOpen,
    projectIndexReady: backendSessionReady,
    project: sessionProject,
    reportError: reportErrorNotice,
  });
  const pinBranchId = graphTarget.kind === "branch" ? graphTarget.branch_id : null;
  const pinScope = pinScopeKey(projectId, pinBranchId);
  const [pinSelection, setPinSelection] = useState<{ scope: string; id: string } | null>(null);
  // A route naming a pin opens it once that target's pins have loaded.
  const [requestedPin, setRequestedPin] = useState(() =>
    pinRequest(
      initialRoute.project.projectId,
      initialRoute.graphTarget,
      initialRoute.project.pinId,
    ),
  );
  const graphView = useResource(
    `${projectId}:graph-view`,
    backendSessionReady && projectId ? graphProfile(projectId, graphTarget) : null,
  );
  const { project: targetProject, humanDraft } = projectDraftPreviewEffectInputs(
    projectSession,
    projectId,
    graphTarget,
  );
  const editOptionsKey = projectId ? graphSessionKey(projectId, graphTarget) : null;
  const [loadedEditOptions, setLoadedEditOptions] = useState<{
    key: string;
    options: GraphEditOptions | null;
  } | null>(null);
  const [editOptionsReload, setEditOptionsReload] = useState(0);
  const editOptions = loadedEditOptions?.key === editOptionsKey ? loadedEditOptions.options : null;
  const editOptionsFailed =
    loadedEditOptions?.key === editOptionsKey && loadedEditOptions.options === null;
  useEffect(() => {
    let cancelled = false;
    setLoadedEditOptions(null);
    if (backendSessionReady && projectId && editOptionsKey) {
      void api<GraphEditOptions>(
        graphTargetUrl(`/api/projects/${projectId}/graph-edit-options`, graphTarget),
      ).then(
        (options) => {
          if (!cancelled) setLoadedEditOptions({ key: editOptionsKey, options });
        },
        () => {
          if (!cancelled) setLoadedEditOptions({ key: editOptionsKey, options: null });
        },
      );
    }
    return () => {
      cancelled = true;
    };
  }, [backendSessionReady, projectId, graphTarget, editOptionsKey, editOptionsReload]);
  // Activation keeps the previous snapshot on screen, inert, until the target
  // paints. The held copy is never cached or drafted against.
  const [heldRefProject, setHeldRefProject] = useState<ProjectSnapshot | null>(null);
  const displayedProjectRef = useRef<ProjectSnapshot | null>(null);
  // A changed backend brings a new, empty store; hold what was on screen
  // until the new backend paints it, as an activation does.
  const [displayedStore, setDisplayedStore] = useState(store);
  if (displayedStore !== store) {
    setDisplayedStore(store);
    setHeldRefProject(displayedProjectRef.current);
  }
  const settingsResource = useProjectResource(
    backendSessionReady && projectId ? settingsProfile(projectId) : null,
  );
  const baseProject =
    targetProject ??
    (sessionProject?.id === projectId
      ? sessionProject
      : heldRefProject?.id === projectId
        ? heldRefProject
        : null);
  const project = useMemo(
    () =>
      baseProject && settingsResource.value
        ? { ...baseProject, ...settingsResource.value }
        : baseProject,
    [baseProject, settingsResource.value],
  );
  displayedProjectRef.current = project;
  const graph = project?.graph ?? emptyGraph;
  const paperResource = useResource(
    `${projectId}:paper-nav`,
    backendSessionReady && projectId ? paperProfile(projectId) : null,
  );
  const paper = paperResource.value?.paper ?? project?.paper ?? null;
  const openMoveProjectSetup = useCallback((sourceProjectId: string) => {
    window.location.hash = projectMoveSetupHash({ sourceProjectId });
  }, []);
  const [textScale, setTextScale] = useState(readTextScale);
  const [spaceSettingsOpen, setSpaceSettingsOpen] = useState(false);
  // Space settings is a page over the current route; any navigation leaves it.
  useEffect(() => setSpaceSettingsOpen(false), [projectId, setupOpen]);
  useEffect(() => closeArtifactViewer(), [projectId]);
  const dockedArtifact = useDockedArtifact();
  // The viewer belongs to the project; a space-level page covers both.
  useEffect(
    () => setArtifactViewerHidden(spaceSettingsOpen || setupOpen),
    [spaceSettingsOpen, setupOpen],
  );
  const appearance = useTheme();
  const phone = useNarrowViewport();
  const [loading, setLoading] = useState(true);
  const [projectReconciliation, setProjectReconciliation] =
    useState<ProjectReconciliation>("opening");
  useEffect(() => {
    // A failed read keeps the last snapshot inert; only a target paint releases it.
    if (heldRefProject && targetProject) setHeldRefProject(null);
  }, [heldRefProject, targetProject]);
  const usage =
    useProjectResource(backendSessionReady && projectId ? usageProfile(projectId) : null).value ??
    null;
  useEffect(() => {
    if (graphView.error && !graphView.value) {
      setLoading(false);
      setProjectReconciliation("failed");
      setNotice({ kind: "error", text: String(graphView.error) });
    }
  }, [graphView.error, graphView.value]);
  // A route naming no branch, or one the backend does not know, opens main and
  // says so, replacing the failed read's notice, instead of a blank target
  // whose every read fails and retries.
  const missingBranchId =
    graphTarget.kind === "branch" &&
    (!graphTarget.branch_id ||
      (graphView.error instanceof ApiError && graphView.error.status === 404))
      ? graphTarget.branch_id
      : null;
  useEffect(() => {
    if (missingBranchId === null || !backendSessionReady) return;
    const [path, query = ""] = window.location.hash.split("?");
    const params = new URLSearchParams(query);
    if (!params.has("branch_id")) return;
    params.delete("branch_id");
    setNotice({
      kind: "error",
      text: missingBranchId
        ? `Branch ${missingBranchId.slice(0, 8)} was not found. Showing main.`
        : "The link names no branch. Showing main.",
    });
    window.location.replace(`${path}${params.size ? `?${params}` : ""}`);
  }, [missingBranchId, backendSessionReady]);
  const watcherView = useResource(
    `${projectId}:watcher-view`,
    backendSessionReady && projectId ? watchersProfile(projectId, graphTarget) : null,
  );
  const watchers = useMemo(() => watcherView.value ?? [], [watcherView.value]);
  const setWatchers = useCallback(
    (update: WatcherRecord[] | ((current: WatcherRecord[]) => WatcherRecord[])) => {
      if (!projectId) return;
      const id = resourceKeyId(watcherResourceKey(projectId, graphTarget));
      const current = store.view<WatcherRecord[]>(id).value ?? [];
      store.write(id, typeof update === "function" ? update(current) : update);
    },
    [store, projectId, graphTarget],
  );
  const {
    view,
    trustView,
    runScope,
    selectedNode,
    companionNode,
    detailFocusTokens,
    selectedExperimentRunId,
    focusExperimentRunId,
    selectedExperimentRoute,
    selectedAutoResearchEpisodeId,
    experimentStopIds,
    watcherCheckId,
    dockedNodeIds,
    dagRelationFocusId,
    panelRef,
    activeDagViewportRef,
    activeResearchTrailRef,
    captureProjectSelection,
    restoreProjectSelection,
    resetProjectSelection,
    applyCanonicalProject,
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
    replaceExactAutoResearchSelection,
    replaceExactExperimentEpisode,
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
    initialExperimentRoute: initialRoute.project.experimentRoute,
    initialAutoResearchEpisodeId: initialRoute.project.autoResearchEpisodeId,
    projectId,
    graphTarget,
    loadedProjectId: project?.id ?? null,
    loading,
    getActiveProjectId,
  });
  // Pins are fetched only where their tabs are shown.
  const pinsProfile =
    backendSessionReady && projectId && (view === "artifacts" || view === "paper")
      ? researchViewsProfile(projectId, pinBranchId)
      : null;
  const pinsResource = useResource("research-view-pins", pinsProfile);
  const pins = pinsResource.value?.pins ?? [];
  const activePin =
    pinSelection?.scope === pinScope
      ? pins.find((pin) => pin.artifact_id === pinSelection.id)
      : undefined;
  const tabRefs = useRef(new Map<string, string>());
  const currentRef = graphTarget.kind === "main" ? "main" : `branch:${graphTarget.branch_id}`;
  if (projectId) tabRefs.current.set(projectId, currentRef);
  // The routed project is open before its first snapshot adds its dock tab.
  const openProjectIds = new Set(openProjectTabs.map((tab) => tab.id));
  if (projectId) openProjectIds.add(projectId);
  refreshBindings.current = {
    open: backendSessionReady
      ? [...openProjectIds].map((id) => ({
          projectId: id,
          currentRef: tabRefs.current.get(id) ?? "main",
        }))
      : [],
    active: backendSessionReady ? projectId : null,
    lostAccess: (id) => {
      closeProjectRoute(id);
      removeProject(id);
      forgetProjectViewport(id);
      store.release(id);
      tabRefs.current.delete(id);
      setNotice({ kind: "error", text: "This project is no longer available." });
    },
  };
  const tabResources = useProjectTabResources(store);
  const {
    tabs: registeredTabs,
    views: retainedViewKeys,
    profiles: retainedViewProfiles,
  } = tabResources;
  const paperEditorStates = useRef(new Map<string, PaperEditorState>());
  useEffect(() => {
    if (!backendSessionReady) return;
    const next = new Set(openProjectTabs.map((tab) => tab.id));
    if (projectId) next.add(projectId);
    for (const id of new Set([...registeredTabs, ...retainedViewProfiles.keys()]))
      if (!next.has(id)) {
        store.release(id);
        store.dropProject(id);
        tabRefs.current.delete(id);
        retainedViewKeys.delete(id);
        retainedViewProfiles.delete(id);
        paperEditorStates.current.delete(id);
      }
    for (const id of next)
      if (backendSessionReady && !registeredTabs.has(id)) store.register(id, taskStatusProfile(id));
    registeredTabs.clear();
    for (const id of next) registeredTabs.add(id);
    scheduler.now();
  }, [
    backendSessionReady,
    openProjectTabs,
    projectId,
    registeredTabs,
    retainedViewKeys,
    retainedViewProfiles,
    scheduler,
    store,
  ]);
  useEffect(() => {
    if (!backendSessionReady) {
      scheduler.stop();
      return;
    }
    scheduler.start();
    return () => scheduler.stop();
  }, [backendSessionReady, scheduler]);
  useLayoutEffect(() => {
    if (backendSessionReady && projectId) scheduler.activate(projectId);
    else store.setActiveProject(null);
  }, [backendSessionReady, currentRef, projectId, scheduler, store]);
  const selectedIndexedExperiment = experimentIndexEntryForRoute(
    experimentLoops,
    projectId,
    selectedExperimentRoute,
  );
  const selectedExperimentUsesIndex = Boolean(
    selectedExperimentRoute &&
    (selectedExperimentRoute.graph_target.kind === "branch" || graphTarget.kind === "branch"),
  );
  const selectedTargetExperiment = selectedExperimentUsesIndex ? selectedIndexedExperiment : null;
  const authoritativeProjectId = useRef<string | null>(null);
  // The last open the project effect ran, and whether a backend reverification
  // has interrupted it since. Recovery on the same backend reconciles that open.
  const lastProjectOpen = useRef<{
    backendKey: string | null;
    projectId: string | null;
    graphTarget: GraphTargetRef;
    setupOpen: boolean;
  } | null>(null);
  const reloadRef = useRef<(includeTasks?: boolean) => Promise<void>>(async () => undefined);
  const authoritativeReloadInFlight = useRef<{
    projectId: string;
    graphTarget: GraphTargetRef;
    request: Promise<void>;
  } | null>(null);
  const initialShowHandshake = useRef(false);
  const currentProjectStateRef = useRef<Omit<ProjectTabUiState, "viewState"> | null>(null);
  const updateProject = useCallback(
    (update: (current: ProjectSnapshot | null) => ProjectSnapshot | null) => {
      const current = getProjectSessionState().project;
      dispatchProjectSession({ kind: "project_replaced", project: update(current) });
    },
    [dispatchProjectSession, getProjectSessionState],
  );
  const apiBase = projectId ? `/api/projects/${encodeURIComponent(projectId)}` : "";
  useEffect(() => setPinSelection(null), [pinScope]);
  useEffect(() => {
    if (view !== "artifacts") {
      setPinSelection(null);
      setRequestedPin(null);
    }
  }, [view]);
  useEffect(() => {
    if (pinSelection?.scope === pinScope && pinsResource.value && !activePin) {
      setPinSelection(null);
    }
  }, [pinSelection, pinScope, pinsResource.value, activePin]);
  // A cached list can predate the pin, so the request waits for a list holding
  // it; the next route change or explicit tab choice replaces the request.
  useEffect(() => {
    if (requestedPin?.scope !== pinScope || !pinsResource.value) return;
    if (pinsResource.value.pins.some((pin) => pin.artifact_id === requestedPin.id)) {
      setPinSelection(requestedPin);
      setRequestedPin(null);
    }
  }, [requestedPin, pinScope, pinsResource.value]);
  const graphPath = useCallback((path: string) => graphTargetUrl(path, graphTarget), [graphTarget]);
  const isActiveGraph = useCallback(
    (id: string) =>
      isActiveProject(id) && sameGraphTarget(activeGraphTargetRef.current, graphTarget),
    [graphTarget, isActiveProject],
  );
  const syncingDraft = projectId
    ? Boolean(
        projectSession.transitionCoordinator.sync_requests[graphSessionKey(projectId, graphTarget)],
      )
    : false;
  const transitionManifest = trustedProjectTransitionManifest(projectSession, projectId);
  const {
    snapshot: agentTasksSnapshot,
    tasksUnavailable,
    retryTasks,
    taskStarting,
    taskActionId,
    taskInspectorLoading,
    activeTask,
    activityTask,
    recordStartedTask,
    presentTask: presentAgentTask,
    selectTaskInspector,
    chooseRetryTask,
    closeRetryTask,
    beginTaskStart,
    beginTaskAction,
    beginTaskRepair,
    resetProjectTasks,
    restoreProjectTasks,
  } = useAgentTasks({
    projectId: backendSessionReady ? projectId : null,
    reportError: reportErrorNotice,
  });
  const { retryTask, tasks: projectTasks, taskInspectorId, inspectedTask } = agentTasksSnapshot;
  const tasks = useMemo(
    () => projectTasks.filter((task) => sameGraphTarget(task.graph_target, graphTarget)),
    [projectTasks, graphTarget],
  );
  const selectedMainExperimentRouteIsCurrent =
    !selectedExperimentRoute ||
    selectedExperimentUsesIndex ||
    mainExperimentRouteMatchesControl(
      selectedExperimentRoute,
      selectedExperimentRunId ? project?.experiment_control[selectedExperimentRunId] : undefined,
    );
  const selectedExperimentOperational =
    view === "execution" && selectedExperimentRunId && selectedMainExperimentRouteIsCurrent
      ? selectedExperimentUsesIndex
        ? (selectedTargetExperiment?.control.operational ?? null)
        : (project?.experiment_control[selectedExperimentRunId]?.operational ?? null)
      : null;
  const selectedExperimentChatId = selectedExperimentOperational?.chat_id ?? null;
  // Indexed loops can belong to any target, so their chats load against the
  // selected episode's graph. Without an exact route the chat id comes from
  // the viewed graph's own projection, so
  // that target stays correct and is the default.
  const selectedExperimentChatTarget =
    selectedExperimentUsesIndex && selectedExperimentRoute
      ? selectedExperimentRoute.graph_target
      : graphTarget;
  const resolveVisibleChatTranscriptIds = useCallback(
    (selectedId: string | null, floatingId: string | null) =>
      visibleChatTranscriptIds(view, selectedId, floatingId, selectedExperimentChatId),
    [selectedExperimentChatId, view],
  );
  const {
    snapshot: chatStateSnapshot,
    visibleChatSummaries,
    inventoryChatSummaries,
    inventoryLoaded,
    selectChat,
    selectCanonicalChat,
    selectListedConversation,
    setFloatingChat,
    reconcileFloatingChat,
    startConversation,
    discardDraft,
    ensureConversation,
    refreshChatSummaries,
    markVisibleChatRead,
    refreshChatReads,
    resetProjectChats,
    restoreProjectChats,
  } = useChatState({
    projectId: backendSessionReady ? projectId : null,
    apiBase,
    graphTarget,
    selectedExperimentChatId,
    selectedExperimentChatTarget,
    isActiveProject: isActiveGraph,
    visibleTranscriptIds: resolveVisibleChatTranscriptIds,
    reportError: reportErrorNotice,
  });
  const { floatingChat, draftConversations, selectedChatId, chatReads, chatTranscripts } =
    chatStateSnapshot;
  const {
    runDialogOpen,
    autoResearchDialogOpen,
    autoResearchStartError,
    episodeAction,
    episodeRefreshError,
    episodes,
    liveAutoResearchEpisodes,
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
  } = useEpisodeDialogs({
    projectId: backendSessionReady ? projectId : null,
    apiBase,
    selectedAutoResearchEpisodeId,
    isActiveProject,
    runsVisible: view === "execution",
  });
  const activeBranchEpisode = branchOwnerEpisode(graphTarget, episodes, experimentLoops);
  // Agents lists every branch's orchestrator, so each needs its branch's current episode.
  const orchestratorEpisodes = useMemo(
    () =>
      episodes.filter(
        (episode) =>
          episode.mode === "auto_research" &&
          episode.episode_id === episode.graph_branch?.current_episode_id,
      ),
    [episodes],
  );
  const refsView = useResource(
    `${projectId}:refs-view`,
    backendSessionReady && projectId ? refsProfile(projectId) : null,
  );
  const projectGraphRefs = refsView.value ?? (refsView.error ? null : undefined);
  const graphRefs = projectId
    ? { projectId, refs: projectGraphRefs ?? null, stale: refsView.stale }
    : null;
  const setGraphRefsReread = useCallback(
    (_update: unknown) => {
      if (projectId) store.invalidate(resourceKeyId(refsProfile(projectId).key));
    },
    [store, projectId],
  );
  const setGraphRefs = useCallback(
    (update: (value: typeof graphRefs) => typeof graphRefs) => {
      if (!projectId) return;
      const id = resourceKeyId(refsProfile(projectId).key);
      const next = update({
        projectId,
        refs: store.view<GraphRef[]>(id).value ?? null,
        stale: false,
      });
      if (next?.refs) store.write(id, next.refs);
    },
    [store, projectId],
  );
  const branchNamesInUse = useMemo(
    () => (projectGraphRefs ?? []).map((ref) => ref.name),
    [projectGraphRefs],
  );
  // On an Auto-research branch the header button continues that branch.
  const continueBranch =
    activeBranchEpisode?.mode === "auto_research"
      ? { name: branchName(projectGraphRefs, graphTarget), episode: activeBranchEpisode }
      : null;
  // The branch diff's merge marks come from the same preview the Merge panel reads.
  const [branchMergePaths, setBranchMergePaths] = useState<MergeDiffPath[] | null>(null);
  const branchMergeKey = activeBranchEpisode
    ? // The whole summary: a delivery changes the receipt and merge state, not the head.
      JSON.stringify([activeBranchEpisode.episode_id, activeBranchEpisode.graph_branch])
    : null;
  useEffect(() => {
    setBranchMergePaths(null);
    if (!activeBranchEpisode || !apiBase) return;
    let current = true;
    loadMergePreview(apiBase, activeBranchEpisode.episode_id, null)
      .then((preview) => {
        if (current) setBranchMergePaths(preview.graph.paths);
      })
      .catch((error) => {
        if (current) console.warn("Branch merge marks are unavailable:", error);
      });
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- branchMergeKey narrows activeBranchEpisode to the fields that warrant a reload
  }, [apiBase, branchMergeKey]);
  const {
    snapshot: projectHistorySnapshot,
    openProjectHistory,
    closeProjectHistory,
    resetProjectHistory,
    restoreProjectHistory,
    dismissHistoryNotices,
  } = useProjectHistory({
    projectId: backendSessionReady ? projectId : null,
    apiBase,
    graphTarget,
    loadedProjectId: project?.id ?? null,
    revision: graph.revision,
    isActiveProject: isActiveGraph,
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
  currentProjectStateRef.current = targetProject
    ? {
        ...serializeProjectSessionTabState(projectSession),
        projectHeaderCollapsed,
        runScope,
        selectedNodeId: selectedNode?.id ?? null,
        companionNodeId: companionNode?.id ?? null,
        detailFocusTokens,
        selectedExperimentRunId,
        focusExperimentRunId,
        selectedExperimentRoute,
        selectedAutoResearchEpisodeId,
        dockedNodeIds,
        ...cloneChatStateSnapshot(chatStateSnapshot),
        dagRelationFocusId,
        taskInspectorId: agentTasksSnapshot.taskInspectorId,
        activityTaskId: agentTasksSnapshot.activityTaskId,
        retryTaskId: agentTasksSnapshot.retryTask?.operation_id ?? null,
        ...cloneProjectHistorySnapshot(projectHistorySnapshot),
        paperEditorState: paperEditorStates.current.get(targetProject.id),
      }
    : null;

  const rememberProjectState = useCallback(
    (id: string | null, leftHash: string = window.location.hash) => {
      if (!id) return;
      // The routed page and target, so a project still loading is remembered too.
      rememberLeftRoute(id, leftHash);
      const current = currentProjectStateRef.current;
      if (!current || current.projectId !== id) return;
      const selection = captureProjectSelection(id, current.graphTarget);
      cacheProjectState(id, {
        ...current,
        ...selection,
        paperEditorState: paperEditorStates.current.get(id),
        ...cloneChatStateSnapshot(current),
        ...cloneProjectHistorySnapshot(current),
      });
    },
    [cacheProjectState, captureProjectSelection, rememberLeftRoute],
  );

  useEffect(() => {
    if (!notice) return;
    const timeoutId = window.setTimeout(() => {
      setNotice((current) => (current === notice ? null : current));
    }, NOTICE_TIMEOUT_MS);
    return () => window.clearTimeout(timeoutId);
  }, [notice]);

  useEffect(() => {
    const proposalIds = projectSession.draftReconciliationDiscardedProposalIds;
    if (proposalIds.length === 0) return;
    setNotice({ kind: "info", text: proposalChoicesClearedNotice(proposalIds) });
    dispatchProjectSession({ kind: "discarded_proposals_consumed" });
  }, [dispatchProjectSession, projectSession.draftReconciliationDiscardedProposalIds]);

  const restoreProjectTabState = useCallback(
    (id: string, state: ProjectTabUiState, requestedRoute?: ProjectHashRoute) => {
      const discardedProposalIds = state.draftReconciliationDiscardedProposalIds;
      const { next } = restoreProjectSessionTab(id, state, {
        consumeDiscardedProposals: true,
      });
      if (!next.project) return;
      const nextGraph = next.project.graph;
      const presented = applyHumanDraft(nextGraph, next.humanDraft);
      cacheProjectState(id, {
        ...state,
        ...serializeProjectSessionTabState(next),
      });
      authoritativeProjectId.current = id;
      restoreProjectHeader(state.projectHeaderCollapsed);
      restoreProjectSelection(
        id,
        nextGraph,
        presented.nodes,
        state,
        requestedRoute,
        next.graphTarget,
      );
      restoreProjectChats(state, presented.nodes);
      restoreProjectTasks(state);
      restoreProjectHistory(state);
      if (state.paperEditorState && !paperEditorStates.current.has(id))
        paperEditorStates.current.set(id, state.paperEditorState);
      setProjectReconciliation("authoritative");
      setLoading(false);
      if (discardedProposalIds.length > 0) {
        setNotice({ kind: "info", text: proposalChoicesClearedNotice(discardedProposalIds) });
      }
    },
    [
      cacheProjectState,
      restoreProjectChats,
      restoreProjectHeader,
      restoreProjectHistory,
      restoreProjectSelection,
      restoreProjectSessionTab,
      restoreProjectTasks,
    ],
  );

  const reload = useCallback(
    async (_includeTasks = true) => {
      if (!projectId) return;
      // Commands invalidate concrete resources; there is no reload bundle.
      await store.refresh(resourceKeyId(graphResourceKey(projectId, graphTarget)));
    },
    [store, projectId, graphTarget],
  );
  reloadRef.current = reload;

  const manifestView = useResource(
    `${projectId}:manifest-view`,
    backendSessionReady && projectId
      ? manifestProfile(projectId, graphTarget, projectSession.transitionManifestExpectedRulesetTag)
      : null,
  );
  useEffect(() => {
    if (!projectId || manifestView.loading) return;
    dispatchProjectSession(
      manifestView.value
        ? { kind: "manifest_valid", project_id: projectId, manifest: manifestView.value }
        : { kind: "manifest_invalid", project_id: projectId },
    );
  }, [
    projectId,
    manifestView.value,
    manifestView.loading,
    dispatchProjectSession,
    projectSession.transitionManifestRefresh,
  ]);

  const reloadAuthoritativeProject = useCallback(
    (requestedProjectId?: string | null) => {
      const activeId = requestedProjectId ?? getActiveProjectId();
      if (!activeId || !isActiveProject(activeId)) return Promise.resolve();
      if (
        authoritativeReloadInFlight.current?.projectId === activeId &&
        sameGraphTarget(
          authoritativeReloadInFlight.current.graphTarget,
          activeGraphTargetRef.current,
        )
      ) {
        return authoritativeReloadInFlight.current.request;
      }
      const request = reloadRef.current().finally(() => {
        if (authoritativeReloadInFlight.current?.request === request) {
          authoritativeReloadInFlight.current = null;
        }
      });
      authoritativeReloadInFlight.current = {
        projectId: activeId,
        graphTarget: activeGraphTargetRef.current,
        request,
      };
      return request;
    },
    [getActiveProjectId, isActiveProject],
  );

  useEffect(() => {
    if (!desktop) return;
    let stopped = false;
    const cleanups: Array<() => void> = [];
    // Revalidate what the window shows: the active project, or the project index.
    // The project stays interactive; the watermark beat this starts refetches
    // whatever changed, and the store retries reads that failed meanwhile.
    const reloadVisibleState = async () => {
      if (stopped || !backendSessionReadyRef.current) return;
      const activeId = getActiveProjectId();
      if (activeId) scheduler.activate(activeId);
      else await loadProjectIndex();
    };
    const prepareShow = async () => {
      try {
        const identity = await reverifyIdentity("prepare-show");
        if (identity.ok) {
          await reloadVisibleState();
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
    // A recovered tunnel revalidates now rather than at the next backoff beat.
    const onTeamTransportRecovered = (event: Event) => {
      const detail = (event as CustomEvent<BackendIdentityEventDetail>).detail;
      if (stopped || !detail.ok || detail.reason !== TEAM_TRANSPORT_RECOVERED) return;
      void reloadVisibleState().catch((error) => {
        if (!stopped)
          setNotice({
            kind: "error",
            text: error instanceof Error ? error.message : String(error),
          });
      });
    };
    window.addEventListener(BACKEND_IDENTITY_EVENT, onTeamTransportRecovered);
    cleanups.push(() =>
      window.removeEventListener(BACKEND_IDENTITY_EVENT, onTeamTransportRecovered),
    );
    void refreshDesktopUpdate();
    return () => {
      stopped = true;
      cleanups.forEach((cleanup) => cleanup());
    };
  }, [
    currentActiveAgentTasks,
    desktop,
    getActiveProjectId,
    recordDesktopUpdateReady,
    loadProjectIndex,
    refreshDesktopUpdate,
    reportIdentityIssue,
    reverifyIdentity,
    scheduler,
  ]);

  const loginResource = useProjectResource(
    backendSessionReady && projectId ? providerLoginsProfile(projectId) : null,
  );
  const liveProviderLogins = loginResource.value ?? null;
  const refreshProviderLogins = useCallback(async () => {
    if (projectId) store.invalidate(resourceKeyId(providerLoginsProfile(projectId).key));
  }, [projectId, store]);
  const runsProviderLogins = useMemo(
    () => mergeProviderLogins(project?.provider_logins ?? [], liveProviderLogins),
    [project?.provider_logins, liveProviderLogins],
  );
  const refreshReadiness = useCallback(async () => {
    if (!projectId) return;
    const id = resourceKeyId(settingsProfile(projectId).key);
    const mutation = store.beginMutation(
      crypto.randomUUID(),
      new Map([[id, (value: unknown) => value]]),
    );
    try {
      const readiness = await loadProjectReadiness(apiBase, true);
      const current = store.view<SettingsResource>(id).value;
      mutation.settle(current ? new Map([[id, { ...current, ...readiness }]]) : undefined);
    } catch (error) {
      mutation.fail();
      throw error;
    }
  }, [apiBase, projectId, store]);

  const refreshUsage = useCallback(async () => {
    if (projectId) store.invalidate(resourceKeyId(usageProfile(projectId).key));
  }, [projectId, store]);

  useLayoutEffect(() => {
    // A new editor supersedes the old one's pending save. Leaving Paper alone
    // still lets that save update the project snapshot used by other views.
    if (view === "paper" && project?.id) {
      dispatchProjectSession({ kind: "paper_editor_opened" });
    }
  }, [dispatchProjectSession, project?.id, view]);

  useEffect(() => {
    const handleHashChange = (event: HashChangeEvent) => {
      // A notification link is not a project route; the resolver below replaces it
      // with one, so the open project stays mounted meanwhile.
      if (isNotificationLinkHash(window.location.hash)) {
        rememberProjectState(getActiveProjectId(), new URL(event.oldURL).hash);
        return;
      }
      const route = parseProjectHash(window.location.hash);
      const activeId = getActiveProjectId();
      const nextTarget = graphTargetFromHash(window.location.hash);
      setRequestedChat({
        hash: window.location.hash,
        projectId: route.projectId,
        chatId: route.chatId,
        graphTarget: nextTarget,
      });
      setRequestedPin(pinRequest(route.projectId, nextTarget, route.pinId));
      // A route that names a chat opens it; any other entry shows the board.
      setAgentsBoard(!route.chatId);
      if (
        route.projectId !== activeId ||
        !sameGraphTarget(nextTarget, activeGraphTargetRef.current)
      ) {
        // The hash already names the next route; the project was left on the old one.
        rememberProjectState(activeId, new URL(event.oldURL).hash);
      }
      activeGraphTargetRef.current = nextTarget;
      setGraphTarget((current) => (sameGraphTarget(current, nextTarget) ? current : nextTarget));
      applyHashRoute(route.projectId, isSetupRoute());
      applyRouteSelection(
        route.view,
        route.experimentId,
        route.experimentRoute,
        route.autoResearchEpisodeId,
      );
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, [applyHashRoute, applyRouteSelection, getActiveProjectId, rememberProjectState]);

  useLayoutEffect(() => {
    if (!backendSessionReady || setupOpen) return;
    if (!projectId) {
      void loadProjectIndex().finally(() => setLoading(false));
      void refreshProjectInvitations();
      return;
    }
    const previous = lastProjectOpen.current;
    if (previous?.backendKey !== backendKey) {
      clearProjectCache();
      paperEditorStates.current.clear();
    }
    lastProjectOpen.current = { backendKey, projectId, graphTarget, setupOpen };
    const profiles: import("./core/resourceStore").ResourceProfile<unknown>[] = [
      graphProfile(projectId, graphTarget),
      refsProfile(projectId),
      watchersProfile(projectId, graphTarget),
      chatsResourceProfile(projectId, apiBase),
      episodesProfile(projectId),
      experimentIndexProfile(projectId),
      historyProfile(projectId, graphTarget),
      manifestProfile(projectId, graphTarget, projectSession.transitionManifestExpectedRulesetTag),
    ];
    if (view === "paper") profiles.push(paperProfile(projectId));
    tabResources.select(projectId, profiles);
    const current = getProjectSessionState();
    if (
      !previous ||
      previous.backendKey !== backendKey ||
      current.projectId !== projectId ||
      !sameGraphTarget(current.graphTarget, graphTarget)
    ) {
      setHeldRefProject(displayedProjectRef.current);
      const retained = cachedProjectStateForOpen(projectId, graphTarget)?.state;
      if (retained) {
        // A bare project route (tab switch) keeps the panel the human left.
        const route = parseProjectHash(window.location.hash);
        restoreProjectTabState(projectId, retained, route.projectViewSpecified ? route : undefined);
      } else {
        let draft: HumanDraft | null = null;
        const key = humanDraftStorageKey(draftActorIdRef.current, projectId, graphTarget);
        try {
          if (key) draft = deserializeHumanDraft(localStorage.getItem(key));
        } catch {}
        dispatchProjectSession({
          kind: "reset",
          project_id: projectId,
          graph_target: graphTarget,
          human_draft: draft,
          invalidate_backend: previous?.backendKey !== backendKey,
        });
        const route = parseProjectHash(window.location.hash);
        resetProjectSelection(
          route.view,
          route.experimentId,
          route.experimentRoute,
          route.autoResearchEpisodeId,
        );
        resetProjectChats();
        // A backend change drops the selection with the cache; the route still
        // names the open chat, so it is requested again rather than lost.
        if (previous && previous.backendKey !== backendKey && route.chatId)
          setRequestedChat({
            hash: window.location.hash,
            projectId,
            chatId: route.chatId,
            graphTarget,
          });
        resetProjectTasks();
        resetProjectHistory(projectId, graphTarget);
        resetProjectHeader(projectId);
      }
    }
    const snapshot = store.view<ProjectSnapshot>(
      resourceKeyId(graphResourceKey(projectId, graphTarget)),
    ).value;
    if (snapshot) {
      setProjectReconciliation("authoritative");
    } else setProjectReconciliation("opening");
    setLoading(!snapshot);
    // Switching only paints retained data and registers demand. The scheduler
    // validates the current ref without blocking this layout pass.
  }, [
    store,
    tabResources,
    backendSessionReady,
    backendKey,
    projectId,
    graphTarget,
    setupOpen,
    view,
    apiBase,
    cachedProjectStateForOpen,
    clearProjectCache,
    dispatchProjectSession,
    getProjectSessionState,
    loadProjectIndex,
    refreshProjectInvitations,
    resetProjectChats,
    resetProjectTasks,
    resetProjectHeader,
    resetProjectHistory,
    resetProjectSelection,
    restoreProjectTabState,
    projectSession.transitionManifestExpectedRulesetTag,
  ]);

  useEffect(() => {
    if (!targetProject || targetProject.id !== projectId) return;
    authoritativeProjectId.current = projectId;
    setLoading(false);
    setProjectReconciliation("authoritative");
    applyCanonicalProject(targetProject, true);
    reconcileFloatingChat(targetProject.graph.nodes, false);
    try {
      persistProjectHumanDraft(
        localStorage,
        draftActorIdRef.current,
        targetProject.id,
        getProjectSessionState().humanDraft,
        graphTarget,
      );
    } catch {
      // The in-memory draft remains available if local storage is unavailable.
    }
  }, [
    targetProject,
    projectId,
    graphTarget,
    applyCanonicalProject,
    reconcileFloatingChat,
    getProjectSessionState,
  ]);

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

  const mutationsDisabled = project ? projectGraphMutationsDisabled(project) : false;
  const {
    consolidation,
    error: consolidationError,
    refresh: refreshConsolidation,
    updateFlag: updateEvidenceFlag,
  } = useConsolidation(backendSessionReady ? apiBase : "", String(graph.revision));
  const refreshProjectIndexQuietly = useCallback(() => {
    // The landing count follows Caught up; a failed index read keeps the old list.
    void loadProjectIndex().catch(() => undefined);
  }, [loadProjectIndex]);
  const {
    digest: projectDigest,
    error: projectDigestError,
    catchingUp: projectDigestCatchingUp,
    catchUp: catchUpProjectDigest,
  } = useProjectDigest(
    backendSessionReady ? projectId : null,
    graphTarget,
    String(graph.revision),
    refreshProjectIndexQuietly,
  );
  const digestChangedNodes = useMemo(
    () => digestChangedNodeIds(projectDigest, graphTarget),
    [projectDigest, graphTarget],
  );
  const candidateTransitionProjection = mutationsDisabled ? null : draftTransitionProjection;
  const retryConfig = useMemo(
    () => (retryTask && project ? taskRetryConfig(retryTask, project) : null),
    [project, retryTask],
  );
  const canonicalAttention = projectAttentionForPresentation(project, null);
  const normalizedPreviewDraft = useMemo(
    () =>
      humanDraft
        ? normalizeHumanDraft(humanDraft, graph, canonicalAttention.stale_proposal_ids)
        : null,
    [graph, humanDraft, canonicalAttention.stale_proposal_ids],
  );
  const draftPreviewRouting = useMemo(
    () =>
      normalizedPreviewDraft
        ? humanDraftTransitionRouting(
            normalizedPreviewDraft,
            graph,
            transitionManifest,
            transitionRulesetTag,
          )
        : ({ route: "local_draft", reason: "no_manifest_trigger" } as const),
    [graph, normalizedPreviewDraft, transitionManifest, transitionRulesetTag],
  );
  const presentedTransitionProjection = transitionProjectionForRoute(
    candidateTransitionProjection,
    draftPreviewRouting.route,
  );
  const presentedExperimentControl =
    presentedTransitionProjection?.experiment_control ?? project?.experiment_control ?? {};
  const experimentControlForNode = (node: GraphNode): ExperimentControlState | null => {
    if (!project || !isControlNode(node.type)) return null;
    const control = presentedExperimentControl[node.id];
    if (!control) {
      throw new Error(`Experiment ${node.id} is missing its backend control projection.`);
    }
    return control;
  };
  const presentedGraph = useMemo(
    () =>
      attentionGraphForProjection(
        graph,
        presentedTransitionProjection,
        draftPreviewRouting.route,
        mutationsDisabled ? null : humanDraft,
      ),
    [
      graph,
      presentedTransitionProjection,
      draftPreviewRouting.route,
      mutationsDisabled,
      humanDraft,
    ],
  );
  const presentedAttention = projectAttentionForPresentation(
    project,
    presentedTransitionProjection,
  );
  const experimentStartRequiresSync = experimentStartNeedsSync(presentedTransitionProjection);
  const attentionGraph = presentedGraph;
  const glossaryIndex = useMemo(
    () => buildGlossaryIndex(presentedGraph.glossary),
    [presentedGraph.glossary],
  );
  const openNodeById = (nodeId: string) => openNode(presentedGraph.nodes[nodeId] ?? null);
  // A notification link resolves once into the item's ordinary route. A graph
  // item then opens as soon as its project's graph holds it, resolved or not.
  // It waits for a verified session, so a link opened before team sign-in
  // still reaches its exact item afterwards.
  const [notificationNode, setNotificationNode] = useState<NotificationLink | null>(null);
  const initialLinkPending = useRef(initialNotificationLink);
  useEffect(() => {
    if (!backendSessionReady) return;
    const resolve = async () => {
      const link = initialLinkPending.current ?? parseNotificationLink(window.location.hash);
      initialLinkPending.current = null;
      if (!link) return;
      setNotificationNode(null);
      const sourceTarget: GraphTargetRef =
        link.target === "main" ? MAIN_GRAPH : { kind: "branch", branch_id: link.target };
      let next = graphNotificationHash(link);
      if (link.kind === "artifact" || link.kind === "paper" || link.kind === "node") {
        next = graphViewHash(
          link.projectId,
          sourceTarget,
          link.kind === "node" ? "dag" : link.kind === "paper" ? "paper" : "artifacts",
        );
        if (link.kind !== "paper") setNotificationNode(link);
      } else if (link.kind === "consolidation") {
        // A consolidation row or schedule notice lives in the Inbox itself.
      } else if (link.kind === "episode") {
        const [episodes, entries] = await Promise.all([
          loadEpisodes(
            `/api/projects/${encodeURIComponent(link.projectId)}`,
            undefined,
            link.itemId,
          ).catch(() => []),
          loadProjectExperimentEpisodes(link.projectId)
            .then((index) => index.entries)
            .catch(() => []),
        ]);
        next = episodeNotificationHash(link, episodes[0] ?? null, entries);
      } else {
        setNotificationNode(link);
      }
      window.location.replace(next);
    };
    void resolve();
    window.addEventListener("hashchange", resolve);
    return () => window.removeEventListener("hashchange", resolve);
  }, [backendSessionReady]);
  // Each signed-in visit keeps the server's phone record in step with this browser.
  const teamSpace = verifiedHealth?.space_kind === "team";
  useEffect(() => {
    if (!teamSpace || desktop) return;
    void Promise.all([loadTeamSessions(), loadNotificationDevices()])
      .then(([sessions, devices]) => {
        const current = sessions.find((session) => session.is_current);
        return reconcileWebPush(
          devices.find((device) => device.session_id === current?.session_id) ?? null,
        );
      })
      .catch(() => {
        // A missed reconciliation is retried on the next visit.
      });
  }, [teamSpace, desktop]);
  useEffect(() => {
    if (!notificationNode || notificationNode.projectId !== projectId || project?.id !== projectId)
      return;
    if (notificationNode.kind === "node" || notificationNode.kind === "artifact") {
      const sourceTarget: GraphTargetRef =
        notificationNode.target === "main"
          ? MAIN_GRAPH
          : { kind: "branch", branch_id: notificationNode.target };
      // Route state updates before the graph snapshot arrives. Never open a
      // same-id node from the previous target while that request is pending.
      if (
        !sameGraphTarget(graphTarget, sourceTarget) ||
        !sameGraphTarget(project.graph_target, sourceTarget)
      )
        return;
      if (notificationNode.kind === "artifact") {
        setNotificationNode(null);
        openArtifact({ projectId, artifactId: notificationNode.itemId });
        return;
      }
    }
    if (notificationNode.kind === "proposal") {
      // Proposals are not graph nodes: a pending one is shown in the Inbox,
      // and a resolved one reports its outcome.
      const proposal = presentedGraph.proposals[notificationNode.itemId];
      if (!proposal) return;
      setNotificationNode(null);
      if (proposal.status === "pending") {
        window.requestAnimationFrame(() =>
          document
            .querySelector(`[data-proposal-id="${CSS.escape(proposal.id)}"]`)
            ?.scrollIntoView({ block: "center" }),
        );
      } else {
        setNotice({ kind: "info", text: `Proposal "${proposal.title}" was ${proposal.status}.` });
      }
      return;
    }
    const node = presentedGraph.nodes[notificationNode.itemId];
    if (!node) return;
    setNotificationNode(null);
    openNode(node);
  }, [
    notificationNode,
    projectId,
    project?.id,
    project?.graph_target,
    graphTarget,
    presentedGraph.nodes,
    presentedGraph.proposals,
    openNode,
  ]);
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
  // Agents spans the project; node actions and composers keep exact-target selection.
  const inventoryConversations = useMemo(
    () =>
      groupChatConversations(
        inventoryChatSummaries,
        projectTasks,
        nodeTitles,
        project?.name ?? "Project",
        draftConversations,
        graphTarget,
      ),
    [
      draftConversations,
      graphTarget,
      inventoryChatSummaries,
      nodeTitles,
      project?.name,
      projectTasks,
    ],
  );
  const conversations = useMemo(
    () =>
      inventoryConversations.filter((conversation) =>
        sameGraphTarget(conversation.graphTarget, graphTarget),
      ),
    [graphTarget, inventoryConversations],
  );
  useEffect(() => {
    if (selectedExperimentChatId && floatingChat?.chatId === selectedExperimentChatId) {
      setFloatingChat(null);
    }
  }, [floatingChat?.chatId, selectedExperimentChatId, setFloatingChat]);
  const draftChangeCount = humanDraftChangeCount(humanDraft);
  const committableDraftCount = humanDraftCommittableCount(humanDraft, graph);
  const behindDraftCount = humanDraftBehindCount(humanDraft, graph);
  const ontologyDraftIsStale = humanDraftOntologyIsStale(humanDraft, graph);
  useLayoutEffect(() => {
    if (mutationsDisabled || !humanDraft) {
      dispatchProjectSession({
        kind: "draft_preview_changed",
        projection: null,
        conflict: null,
        pending: false,
      });
      return;
    }
    if (committableDraftCount === 0 || ontologyDraftIsStale) {
      dispatchProjectSession({
        kind: "draft_preview_changed",
        projection: draftTransitionProjection,
        conflict: ontologyDraftIsStale
          ? "The staged ontology is based on an older canonical revision. Restage or reset it before previewing or Sync."
          : "The remaining staged node edits are behind canonical state. Reconcile or reset them before previewing or Sync.",
        pending: false,
      });
      return;
    }
    if (draftPreviewRouting.route === "local_draft") {
      dispatchProjectSession({
        kind: "draft_preview_changed",
        projection: localDraftTransitionProjection(
          applyHumanDraft(graph, humanDraft),
          project?.experiment_control ?? {},
          projectAttentionForPresentation(project, null),
          project?.primary_question ?? null,
          project?.counts ?? emptyProjectCounts(),
          transitionHead,
          transitionRulesetTag,
        ),
        conflict: null,
        pending: false,
      });
      return;
    }
    dispatchProjectSession({
      kind: "draft_preview_changed",
      projection: draftTransitionProjection,
      conflict: null,
      pending: true,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- draftTransitionProjection is this effect's own output (a dep would loop the local-draft path); project is narrowed to the fields not already carried by graph
  }, [
    committableDraftCount,
    dispatchProjectSession,
    draftPreviewRouting.route,
    graph,
    humanDraft,
    mutationsDisabled,
    ontologyDraftIsStale,
    project?.experiment_control,
    project?.attention,
    transitionHead,
    transitionRulesetTag,
  ]);

  useEffect(() => {
    if (
      !apiBase ||
      !projectId ||
      !project ||
      projectReconciliation !== "authoritative" ||
      !normalizedPreviewDraft ||
      committableDraftCount === 0 ||
      ontologyDraftIsStale ||
      mutationsDisabled ||
      draftPreviewRouting.route !== "backend_preview"
    )
      return;
    const requestedProjectId = projectId;
    const request = toHumanSyncRequest(normalizedPreviewDraft, graph);
    // A superseded preview is aborted, so refetches never stack replays on the server.
    const controller = new AbortController();
    void api<TransitionPreviewResponse>(
      graphPath(`${apiBase}/sync/preview`),
      { method: "POST", body: JSON.stringify(request), signal: controller.signal },
      { readOnly: true },
    )
      .then((response) => {
        if (controller.signal.aborted || !isActiveGraph(requestedProjectId)) return;
        const manifest = trustedProjectTransitionManifest(
          getProjectSessionState(),
          requestedProjectId,
        );
        const projection = decodeProjectTransitionResponse(response.projection);
        const previewBaseHead = projection.base_head;
        if (!previewBaseHead) {
          dispatchProjectSession({
            kind: "draft_preview_changed",
            projection: getProjectSessionState().draftTransitionProjection,
            conflict: "Staged transition preview omitted its canonical base head.",
            pending: false,
          });
          return;
        }
        const traceMismatch = previewTraceMismatch(response, projection);
        if (traceMismatch) {
          dispatchProjectSession({
            kind: "draft_preview_changed",
            projection: getProjectSessionState().draftTransitionProjection,
            conflict: traceMismatch,
            pending: false,
          });
          return;
        }
        const currentProjection: ProjectTransitionProjection<
          GraphState,
          Record<string, ExperimentControlState>
        > = {
          head: transitionHead,
          graph,
          attention: project.attention,
          experiment_control: project.experiment_control,
          primary_question: project.primary_question ?? null,
          counts: project.counts,
          ruleset_tag: transitionRulesetTag,
          transition_id: transitionHead.transition_id,
          canonical: true,
        };
        const structuralRefusal = transitionSnapshotRefusal(currentProjection, {
          kind: "preview",
          snapshot: projection,
          expected_base_head: transitionHead,
          manifest_ruleset_tag: null,
        });
        if (structuralRefusal) {
          dispatchProjectSession({
            kind: "draft_preview_changed",
            projection: getProjectSessionState().draftTransitionProjection,
            conflict: `Staged transition preview was refused: ${structuralRefusal}.`,
            pending: false,
          });
          return;
        }
        if (
          (transitionRulesetTag && transitionRulesetTag !== projection.ruleset_tag) ||
          (manifest && manifest.ruleset_tag !== projection.ruleset_tag)
        ) {
          dispatchProjectSession({
            kind: "preview_ruleset_invalidated",
            project_id: requestedProjectId,
            head: previewBaseHead,
            ruleset_tag: projection.ruleset_tag,
            manifest,
          });
          return;
        }
        const matchingManifestTag =
          manifest?.ruleset_tag === transitionRulesetTag ? manifest.ruleset_tag : null;
        const refusal = transitionSnapshotRefusal(currentProjection, {
          kind: "preview",
          snapshot: projection,
          expected_base_head: transitionHead,
          manifest_ruleset_tag: matchingManifestTag,
        });
        if (refusal) {
          dispatchProjectSession({
            kind: "draft_preview_changed",
            projection: getProjectSessionState().draftTransitionProjection,
            conflict: `Staged transition preview was refused: ${refusal}.`,
            pending: false,
          });
          return;
        }
        const next = reduceProjectTransitionProjection(currentProjection, {
          kind: "preview",
          snapshot: projection,
          expected_base_head: transitionHead,
          manifest_ruleset_tag: matchingManifestTag,
        });
        dispatchProjectSession({
          kind: "preview_applied",
          project_id: requestedProjectId,
          projection: next,
          base_head: previewBaseHead,
        });
      })
      .catch((error) => {
        if (controller.signal.aborted || !isActiveGraph(requestedProjectId)) return;
        dispatchProjectSession({
          kind: "draft_preview_changed",
          projection: getProjectSessionState().draftTransitionProjection,
          conflict: error instanceof Error ? error.message : String(error),
          pending: false,
        });
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the project changes with any field and a refetched manifest is a new object; only the fields the preview reads rerun it
  }, [
    apiBase,
    graphPath,
    isActiveGraph,
    committableDraftCount,
    dispatchProjectSession,
    draftPreviewRouting.route,
    graph,
    getProjectSessionState,
    isActiveProject,
    mutationsDisabled,
    normalizedPreviewDraft,
    ontologyDraftIsStale,
    project?.attention,
    project?.experiment_control,
    project?.primary_question,
    project?.counts,
    projectId,
    projectReconciliation,
    transitionHead,
    transitionManifest?.ruleset_tag,
    transitionRulesetTag,
  ]);
  // Only a listed conversation can be opened and so marked read; counting any
  // other chat would leave the badge stuck.
  const readUnreadChatIds = useMemo(
    () => unreadChatIdsFromReads(projectTasks, chatReads),
    [chatReads, projectTasks],
  );
  const listedConversations = useMemo(
    () =>
      withEpisodeAttention(
        listedAgentConversations(
          inventoryConversations,
          graphTarget,
          new Set(orchestratorEpisodes.map((episode) => episode.graph_target.branch_id ?? "")),
        ),
        orchestratorEpisodes,
      ),
    [inventoryConversations, graphTarget, orchestratorEpisodes],
  );
  const unreadChatIds = useMemo(
    () =>
      new Set(
        listedConversations
          .filter((conversation) => conversationHasUnread(conversation, readUnreadChatIds))
          .map((conversation) => conversation.chatId),
      ),
    [listedConversations, readUnreadChatIds],
  );
  const chatsIndicator = chatIndicator(listedConversations, unreadChatIds);

  const changeAppTextScale = (action: TextScaleAction) => {
    setTextScale((current) => changeTextScale(current, action));
  };

  const openChats = (preferredChatId?: string | null) => {
    setAgentsBoard(preferredChatId == null);
    const nextChatId =
      preferredChatId ??
      chatEntryConversationId(conversations, activityTask, unreadChatIds, selectedChatId);
    selectChat(nextChatId);
    setFloatingChat(null);
    clearNodeSelections();
    changeView("chats");
  };

  // Leaving Agents resets it to the board, so only a link that names a chat skips it.
  useEffect(() => {
    if (view !== "chats") setAgentsBoard(true);
  }, [view]);

  // The route names the open chat, so a project tab returns to the chat it left.
  const openChatId = view === "chats" && !agentsBoard ? (selectedChatId ?? undefined) : undefined;
  const pendingChatId = requestedChat.chatId;
  useEffect(() => {
    if (view !== "chats" || !project?.id || pendingChatId) return;
    const hash = window.location.hash;
    const route = parseProjectHash(hash);
    if (route.projectId !== project.id || (route.chatId ?? undefined) === openChatId) return;
    const [path, query = ""] = hash.split("?");
    const params = new URLSearchParams(query);
    if (openChatId) params.set("chat", openChatId);
    else params.delete("chat");
    window.history.replaceState(window.history.state, "", `${path}?${params}`);
  }, [view, project?.id, openChatId, pendingChatId]);

  // The request's own answer selects a chat, which changes the merged summaries;
  // read them through a ref so that answer never cancels and reissues the request.
  const inventoryChatSummariesRef = useRef(inventoryChatSummaries);
  inventoryChatSummariesRef.current = inventoryChatSummaries;
  useEffect(() => {
    if (
      !requestedChat.chatId ||
      !inventoryLoaded ||
      loading ||
      view !== "chats" ||
      !backendSessionReady ||
      project?.id !== requestedChat.projectId ||
      !sameGraphTarget(graphTarget, requestedChat.graphTarget)
    )
      return;
    const finish = () =>
      setRequestedChat((current) =>
        current === requestedChat ? { ...current, chatId: undefined } : current,
      );
    // A listed chat is read by its own selected resource; a separate read here would
    // fence that one and make the store confirm the written answer with a third.
    const listed = inventoryChatSummariesRef.current.find(
      (item) => item.chat_id === requestedChat.chatId,
    );
    // A link names the chat; the inventory knows its graph. A wrong or missing
    // branch_id reroutes to that graph instead of opening another chat.
    if (
      listed &&
      listed.conversation_kind !== "auto_research" &&
      !sameGraphTarget(listed.graph_target, graphTarget)
    ) {
      window.location.replace(
        projectViewHash(requestedChat.projectId, listed.graph_target, "chats", {
          chatId: listed.chat_id,
        }),
      );
      return;
    }
    if (listed && sameGraphTarget(listed.graph_target, graphTarget)) {
      selectChat(listed.chat_id);
      setFloatingChat(null);
      clearNodeSelections();
      finish();
      return;
    }
    let cancelled = false;
    void loadChatTranscript(apiBase, requestedChat.chatId, api, graphTarget)
      .then((transcript) => {
        if (cancelled) return;
        selectCanonicalChat(transcript);
        clearNodeSelections();
      })
      .catch((error) => {
        if (cancelled) return;
        // A chat that cannot be opened leaves nothing selected, never a retained one.
        selectChat(null);
        const message = `Conversation could not be opened: ${String(error)}`;
        reportErrorNotice(message);
      })
      .finally(() => {
        if (!cancelled) finish();
      });
    return () => {
      cancelled = true;
    };
  }, [
    requestedChat,
    inventoryLoaded,
    selectChat,
    setFloatingChat,
    loading,
    view,
    backendSessionReady,
    project?.id,
    graphTarget,
    apiBase,
    selectCanonicalChat,
    clearNodeSelections,
    reportErrorNotice,
  ]);

  useEffect(() => {
    if (mutationsDisabled) {
      closeRunDialog();
      closeAutoResearchDialog();
    }
  }, [closeAutoResearchDialog, closeRunDialog, mutationsDisabled]);

  // A conversation a task names but the inventory lacks has unknown ownership,
  // so its composer stays closed; fetch the inventory once to resolve it.
  const unlistedChatIds = useMemo(() => {
    if (!inventoryLoaded) return "";
    const listed = new Set(inventoryChatSummaries.map((summary) => summary.chat_id));
    const unlisted = new Set<string>();
    for (const task of projectTasks) {
      const chatId = task.request?.chat_id;
      if (
        (task.kind === "node_chat" || task.kind === "project_chat") &&
        typeof chatId === "string" &&
        !listed.has(chatId)
      )
        unlisted.add(chatId);
    }
    return [...unlisted].sort().join(",");
  }, [inventoryChatSummaries, inventoryLoaded, projectTasks]);
  useEffect(() => {
    if (!unlistedChatIds || !projectId) return;
    void refreshChatSummaries(projectId, apiBase).catch((error) => {
      setNotice({
        kind: "error",
        text: `Chats could not be refreshed: ${error instanceof Error ? error.message : String(error)}`,
      });
    });
  }, [apiBase, projectId, refreshChatSummaries, unlistedChatIds]);

  useEffect(() => {
    const visibleChatId = visibleUnreadChatId(
      view,
      selectedChatId,
      selectedExperimentChatId,
      agentsBoard,
    );
    // Project-wide, as unread is: an orchestrator opened from main has its turns on its branch.
    markVisibleChatRead(projectTasks, visibleChatId);
    // chatReads is a dependency so a chat already open when the markers load is marked.
  }, [
    agentsBoard,
    chatReads,
    markVisibleChatRead,
    projectTasks,
    selectedChatId,
    selectedExperimentChatId,
    view,
  ]);

  const pendingProposals = useMemo(
    () =>
      presentedAttention.pending_proposal_ids.map((proposalId) => {
        const proposal = attentionGraph.proposals[proposalId];
        if (!proposal) {
          throw new Error(`Attention references missing presented Proposal ${proposalId}.`);
        }
        return proposal;
      }),
    [attentionGraph.proposals, presentedAttention.pending_proposal_ids],
  );
  const attentionDecisions = useMemo(
    () =>
      decisionsAwaitingChoice(
        presentedAttention.decisions_awaiting_choice_ids,
        attentionGraph.nodes,
        presentedGraph.nodes,
      ),
    [attentionGraph.nodes, presentedAttention.decisions_awaiting_choice_ids, presentedGraph.nodes],
  );
  const openBlockers = useMemo(
    () => humanAttentionBlockers(presentedAttention.open_blocker_ids, presentedGraph.nodes),
    [presentedAttention.open_blocker_ids, presentedGraph.nodes],
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

  const updateHumanDraft = (
    update: (draft: HumanDraft) => HumanDraft,
    editStep?: { phase: "start" | "finish"; key: string },
  ) => {
    if (!projectId || mutationsDisabled) return;
    setNotice(null);
    const { next } = updateProjectHumanDraft(projectId, graph, update, editStep);
    try {
      persistProjectHumanDraft(localStorage, draftActorId, projectId, next.humanDraft, graphTarget);
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
  };

  const resetHumanDraft = () => {
    if (!projectId) return;
    dispatchProjectSession({ kind: "human_draft_updated", project_id: projectId, draft: null });
    try {
      const key = humanDraftStorageKey(draftActorId, projectId, graphTarget);
      if (key) localStorage.removeItem(key);
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
  };

  const syncHumanDraft = async () => {
    if (
      !projectId ||
      !project ||
      projectReconciliation !== "authoritative" ||
      !humanDraft ||
      syncingDraft ||
      draftPreviewPending ||
      draftPreviewConflict ||
      ontologyDraftIsStale ||
      mutationsDisabled
    )
      return;
    const requestedProjectId = projectId;
    const expectedGraph = graph;
    const expectedProject = project;
    const expectedHead = transitionHead;
    const normalized = normalizeHumanDraft(
      humanDraft,
      graph,
      canonicalAttention.stale_proposal_ids,
    );
    if (humanDraftCommittableCount(normalized, graph) === 0) return;
    const request = toHumanSyncRequest(normalized, graph);
    const fence = beginProjectSync(requestedProjectId, expectedHead);
    if (!fence) return;
    setNotice(null);
    let committedResponseReceived = false;
    const reconcileRequestedProject = async () => {
      if (isActiveGraph(requestedProjectId)) {
        await reloadAuthoritativeProject(requestedProjectId);
      } else {
        // The saved draft is reconciled against its exact target on next open.
        if (expectedHead.target.kind === "main")
          store.invalidate(resourceKeyId(graphResourceKey(requestedProjectId)));
        scheduler.now();
      }
    };
    try {
      const response = await api<ProjectTransitionResponse>(graphPath(`${apiBase}/sync`), {
        method: "POST",
        body: JSON.stringify(request),
      });
      committedResponseReceived = true;
      dispatchProjectSession({
        kind: "activate",
        project_id: getActiveProjectId(),
        graph_target: activeGraphTargetRef.current,
      });
      const disposition = transitionSyncCompletionDisposition(
        getProjectSessionState().transitionCoordinator,
        fence,
      );
      if (
        disposition !== "apply" ||
        getProjectSessionState().renderedRevision !== fence.expected_head.revision
      ) {
        try {
          await reconcileRequestedProject();
          if (isActiveGraph(requestedProjectId)) {
            setNotice({ kind: "info", text: "Sync committed and canonical state was refreshed." });
          }
        } catch (error) {
          if (isActiveGraph(requestedProjectId)) {
            setNotice({
              kind: "error",
              text: `Sync committed, but canonical state could not be refreshed: ${error instanceof Error ? error.message : String(error)}`,
            });
          }
        }
        return;
      }
      const projection = decodeProjectTransitionResponse(response);
      if (projection.head.revision !== expectedHead.revision + 1) {
        throw new Error("Committed transition response did not advance exactly one revision.");
      }
      const currentProjection: ProjectTransitionProjection<
        GraphState,
        Record<string, ExperimentControlState>
      > = {
        head: expectedHead,
        graph: expectedGraph,
        attention: expectedProject.attention,
        experiment_control: expectedProject.experiment_control,
        primary_question: expectedProject.primary_question ?? null,
        counts: expectedProject.counts,
        ruleset_tag: transitionRulesetTag,
        transition_id: expectedHead.transition_id,
        canonical: true,
      };
      const refusal = transitionSnapshotRefusal(currentProjection, {
        kind: "canonical",
        snapshot: projection,
      });
      if (refusal) throw new Error(`Committed transition response was refused: ${refusal}.`);
      const committed = reduceProjectTransitionProjection(currentProjection, {
        kind: "canonical",
        snapshot: projection,
      }) as ProjectTransitionResponse;
      const nextGraph = committed.graph;
      const { previous: previousSession, next: committedSession } = dispatchProjectSession({
        kind: "committed_transition_applied",
        project_id: requestedProjectId,
        projection: committed,
        submitted_draft: normalized,
      });
      if (
        committedSession === previousSession ||
        committedSession.project?.id !== requestedProjectId ||
        committedSession.renderedRevision !== nextGraph.revision
      ) {
        throw new Error("Committed transition response lost the active project session.");
      }
      try {
        persistProjectHumanDraft(
          localStorage,
          draftActorIdRef.current,
          requestedProjectId,
          committedSession.humanDraft,
          expectedHead.target,
        );
      } catch (error) {
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      }
      applyCanonicalProject(committedSession.project, true);
      reconcileFloatingChat(nextGraph.nodes, false);
      if (expectedHead.target.kind === "branch") await reload();
      setNotice({
        kind: "info",
        text: humanSyncSuccessNotice(nextGraph.revision, request.proposals, nextGraph),
      });
    } catch (error) {
      if (committedResponseReceived) {
        try {
          await reconcileRequestedProject();
        } catch (reloadError) {
          if (isActiveGraph(requestedProjectId)) {
            setNotice({
              kind: "error",
              text: `Sync committed, but its response was refused and canonical refresh failed: ${reloadError instanceof Error ? reloadError.message : String(reloadError)}`,
            });
          }
          return;
        }
        if (isActiveGraph(requestedProjectId)) {
          setNotice({
            kind: "error",
            text: `Sync committed, but its response could not be applied directly: ${error instanceof Error ? error.message : String(error)}`,
          });
        }
        return;
      }
      const failure = humanSyncFailure(error);
      if (isActiveGraph(requestedProjectId)) {
        setNotice({ kind: "error", text: failure.text });
      }
      if (failure.revisionConflict) {
        try {
          await reconcileRequestedProject();
        } catch {}
      }
    } finally {
      dispatchProjectSession({ kind: "sync_finished", fence });
    }
  };

  const startAgentTask = useCallback(
    async (
      kind: AgentTaskKind,
      request: AgentTaskRequest,
      requestId?: string,
      // A conversation opened in place from another graph posts to its own graph.
      target: GraphTargetRef = graphTarget,
    ): Promise<AgentTask> => {
      const finishTaskStart = beginTaskStart();
      if (!finishTaskStart) throw new Error("Another task start is already being submitted.");
      const admissionId = requestId ?? crypto.randomUUID();
      const mutation =
        projectId && (kind === "node_chat" || kind === "project_chat")
          ? beginChatSend(store, projectId, target, kind, request, admissionId)
          : projectId
            ? beginTaskPublication(store, projectId)
            : null;
      try {
        const task = await api<AgentTask>(
          kind === "node_chat" || kind === "project_chat"
            ? graphTargetUrl(`${apiBase}/tasks/${kind}`, target)
            : `${apiBase}/tasks/${kind}`,
          {
            method: "POST",
            body: JSON.stringify(request),
            headers: { "Idempotency-Key": admissionId },
          },
          // A keyed start can be sent again safely, so a lost request ends
          // instead of holding the start lock with no answer.
          { attemptTimeoutMs: IDEMPOTENT_TASK_START_TIMEOUT_MS },
        );
        if (
          (kind === "node_chat" || kind === "project_chat") &&
          !sameGraphTarget(task.graph_target, target)
        )
          throw new Error("Conversation start returned a different graph target.");
        mutation?.settle(task);
        if (projectId && isActiveGraph(projectId)) {
          recordStartedTask(task);
          setNotice(null);
        }
        return task;
      } catch (error) {
        mutation?.fail(error);
        throw error;
      } finally {
        finishTaskStart();
      }
    },
    [apiBase, graphTarget, projectId, isActiveGraph, beginTaskStart, recordStartedTask, store],
  );
  const loadWebMcpConversation = useCallback(
    (chatId: string) => loadChatTranscript(apiBase, chatId, api, graphTarget),
    [apiBase, graphTarget],
  );
  const loadWebMcpTask = useCallback(
    async (operationId: string): Promise<AgentTask | null> => {
      try {
        return await api<AgentTask>(`${apiBase}/tasks/${encodeURIComponent(operationId)}`);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    [apiBase],
  );
  const webMcpConversationSource = useMemo(
    () => ({ loadTranscript: loadWebMcpConversation, loadTask: loadWebMcpTask }),
    [loadWebMcpConversation, loadWebMcpTask],
  );
  const loadWebMcpEpisode = useCallback(
    async (episodeId: string): Promise<Episode | null> => {
      try {
        return (await loadEpisodes(apiBase, undefined, episodeId))[0] ?? null;
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    [apiBase],
  );
  const loadWebMcpSavedArtifacts = useCallback(
    () => api<ProjectArtifact[]>(`${apiBase}/artifacts`),
    [apiBase],
  );
  const webMcpArtifactSource = useMemo(
    () => ({
      loadEpisode: loadWebMcpEpisode,
      loadTask: loadWebMcpTask,
      loadSavedArtifacts: loadWebMcpSavedArtifacts,
    }),
    [loadWebMcpEpisode, loadWebMcpTask, loadWebMcpSavedArtifacts],
  );
  const createWebMcpConversation = useCallback(
    (kind: ChatKind, node: GraphNode | null) => {
      if (!project) throw new Error("No RCP project is open.");
      return startConversation(kind, node, project.name);
    },
    [project, startConversation],
  );
  const startWebMcpConversationTurn = useCallback(
    (submission: ConversationTurnSubmission, requestId?: string) =>
      startConversationTurn(
        (kind, request) => startAgentTask(kind, request, requestId),
        submission,
      ),
    [startAgentTask],
  );

  const stopWatcher = async (watcherId: string) => {
    if (!apiBase) return;
    try {
      await api<WatcherRecord>(`${apiBase}/watchers/${encodeURIComponent(watcherId)}/stop`, {
        method: "POST",
      });
      if (projectId) store.invalidate(resourceKeyId(watcherResourceKey(projectId, graphTarget)));
      // Retiring an observer can release an Experiment that was held shut by it,
      // so the control projection is re-read here rather than waiting for a poll.
      await reload();
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
  };

  const requestExperimentStop = useCallback(
    async (nodeId: string, episodeId: string): Promise<void> => {
      if (!apiBase) throw new Error("No RCP project is open.");
      if (!episodeId) throw new Error("An exact episode is required to stop an Experiment loop.");
      if (experimentStopIds.has(episodeId))
        throw new Error("This Experiment Stop is already being submitted.");
      const finishExperimentStop = beginExperimentStop(episodeId);
      const stopping = projectTasks
        .filter((task) => task.episode_id === episodeId && isActiveTask(task))
        .map((task) => ({
          task,
          mutation: beginTaskMutation(store, task.project_id, task, "stop"),
        }));
      try {
        // The exact-episode endpoint resolves its own target, independently of this page.
        await api<unknown>(experimentStopPath(apiBase, nodeId, episodeId), {
          method: "POST",
        });
        for (const { mutation } of stopping) mutation.settle();
        scheduler.now();
        try {
          await refreshExperimentLoops();
        } catch (error) {
          setNotice({
            kind: "error",
            text: `Stop was requested, but Runs could not refresh: ${error instanceof Error ? error.message : String(error)}`,
          });
        }
      } catch (error) {
        for (const { mutation } of stopping) mutation.fail(error);
        throw error;
      } finally {
        finishExperimentStop();
      }
    },
    [
      apiBase,
      beginExperimentStop,
      experimentStopIds,
      refreshExperimentLoops,
      projectTasks,
      scheduler,
      store,
    ],
  );
  const stopExperimentLoop = useCallback(
    async (nodeId: string, episodeId: string) => {
      try {
        await requestExperimentStop(nodeId, episodeId);
      } catch (error) {
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      }
    },
    [requestExperimentStop],
  );

  const checkExperimentWatcher = async (watcherId: string) => {
    if (!apiBase || watcherCheckId || taskStarting || taskActionId || mutationsDisabled) return;
    const watcher = watchers.find((candidate) => candidate.watcher_id === watcherId);
    if (watcher?.episode_id && experimentStopIds.has(watcher.episode_id)) return;
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
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    } finally {
      finishWatcherCheck();
    }
  };

  const startExperiment = useCallback(
    async (
      node: GraphNode,
      invocationCeiling?: number,
      isolation?: EpisodeIsolationChoice,
      browserRequested = false,
      requestId?: string,
      launchConfig?: AgentRunConfig,
      message?: string,
    ): Promise<ExperimentStartResponse> => {
      if (!project || !isControlNode(node.type)) {
        throw new Error("The requested Experiment is not present in the open project.");
      }
      if (mutationsDisabled) throw new Error("Graph mutations are currently disabled.");
      if (experimentStartRequiresSync) {
        throw new Error("Sync staged graph changes before starting an episode.");
      }
      const control = project.experiment_control?.[node.id];
      if (!canStartExperiment(control, graphTarget, isolation?.graph_isolation)) {
        throw new Error(control?.reasons.join(" ") ?? "This experiment is not ready to run.");
      }
      const finishTaskStart = beginTaskStart();
      if (!finishTaskStart) throw new Error("Another task start is already being submitted.");
      const mutation = beginTaskPublication(store, project.id);
      try {
        // A fresh Run is its own conversation. Reusing the node's newest chat
        // would file the episode inside a human's finished Work chat.
        const chatId = window.crypto.randomUUID();
        // A human start may pick the agent; the graph agent still runs on the
        // profile's machine.
        const profile = project.agent_profiles.node_chat;
        const agent = launchConfig ?? profile;
        const task = await startExperimentRun(
          graphPath(`${apiBase}/experiments/${encodeURIComponent(node.id)}/run`),
          {
            provider: agent.provider,
            model: agent.model || null,
            reasoning: agent.reasoning,
            run_on: profile.run_on,
            run_truth_scope: runScope.length ? runScope : project.default_run_truth_scope,
            chat_id: chatId,
            // Omitted unless the human reauthorized an explicit count; the
            // backend then keeps the Experiment node's own limit.
            ...(invocationCeiling === undefined ? {} : { invocation_ceiling: invocationCeiling }),
            ...isolation,
            browser_requested: browserRequested,
            // A blank message lets the backend write the canonical start goal.
            ...(message?.trim() ? { message } : {}),
          },
          requestId,
        );
        const expectedTarget = experimentStartTarget(
          graphTarget,
          isolation?.graph_isolation,
          task.episode_id,
        );
        if (!sameGraphTarget(task.graph_target, expectedTarget))
          throw new Error("Experiment start returned a different graph target.");
        mutation.settle(task);
        if (!isActiveGraph(project.id)) return task;
        recordStartedTask(task);
        setExperimentStartOverlap({ projectId: project.id, loops: task.live_elsewhere });
        setNotice(null);
        setFloatingChat(null);
        if (!task.episode_id) throw new Error("Experiment start returned no episode.");
        showExperiment(node.id, {
          experiment_id: node.id,
          episode_id: task.episode_id,
          graph_target: task.graph_target,
          // A human start is independent of any Auto-research parent on this branch.
          parent_episode_id: null,
        });
        try {
          await Promise.all([
            reload(),
            refreshEpisodes(),
            refreshProjectExperimentLoops(project.id),
          ]);
        } catch (error) {
          setNotice({
            kind: "error",
            text: `The Experiment started, but Runs could not refresh: ${error instanceof Error ? error.message : String(error)}`,
          });
        }
        return task;
      } catch (error) {
        mutation.fail();
        throw error;
      } finally {
        finishTaskStart();
      }
    },
    [
      store,
      apiBase,
      graphPath,
      graphTarget,
      isActiveGraph,
      beginTaskStart,
      experimentStartRequiresSync,
      mutationsDisabled,
      project,
      recordStartedTask,
      refreshProjectExperimentLoops,
      refreshEpisodes,
      reload,
      runScope,
      setFloatingChat,
      showExperiment,
    ],
  );
  // Kept with the node whose start was refused, so another Experiment's drawer never shows it.
  const [experimentBranchRefusal, setExperimentBranchRefusal] = useState<{
    nodeId: string;
    refusal: string | null;
  } | null>(null);
  const [autoResearchBranchRefusal, setAutoResearchBranchRefusal] = useState<string | null>(null);
  const runExperiment = useCallback(
    async (
      node: GraphNode,
      invocationCeiling?: number,
      isolation?: EpisodeIsolationChoice,
      browserRequested = false,
      launchConfig?: AgentRunConfig,
      message?: string,
    ) => {
      setExperimentBranchRefusal(null);
      try {
        await startExperiment(
          node,
          invocationCeiling,
          isolation,
          browserRequested,
          undefined,
          launchConfig,
          message,
        );
      } catch (caught) {
        setExperimentBranchRefusal({ nodeId: node.id, refusal: branchNameRefusal(caught) });
        setNotice({
          kind: "error",
          text: caught instanceof Error ? caught.message : String(caught),
        });
      }
    },
    [startExperiment],
  );
  const startWebMcpExperiment = useCallback(
    async (
      node: GraphNode,
      invocationCeiling?: number,
      requestId?: string,
    ): Promise<ExperimentStartResponse> => {
      if (!project) throw new Error("No RCP project is open.");
      const pendingProjectId = project.id;
      setWebMcpExperimentStartProjectId(pendingProjectId);
      try {
        return await startExperiment(node, invocationCeiling, undefined, false, requestId);
      } finally {
        setWebMcpExperimentStartProjectId((current) =>
          current === pendingProjectId ? null : current,
        );
      }
    },
    [project, startExperiment],
  );

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

  const autoResearchRefusal = autoResearchStartRefusal({
    projectOpen: Boolean(project && apiBase),
    mutationsDisabled,
    authoritative: projectReconciliation === "authoritative",
    canonicalReachable: Boolean(project?.canonical_state.reachable),
    taskStarting,
    episodeAction,
  });
  // The one Auto-research start: the header form and the WebMCP tool both call it.
  const startAutoResearch = useCallback(
    async (
      invocationCeiling: number,
      startingInstruction: string | null,
      codeWorktree = true,
      browserRequested = false,
      requestId?: string,
      launchConfig?: AgentRunConfig,
      branchName: string | null = null,
      bulletinEnabled = true,
    ): Promise<Episode> => {
      if (autoResearchRefusal) throw new Error(autoResearchRefusal);
      const finishTaskStart = beginTaskStart();
      if (!finishTaskStart) throw new Error("Another task start is already being submitted.");
      const finishEpisodeAction = beginEpisodeAction("start");
      if (!finishEpisodeAction) {
        finishTaskStart();
        throw new Error("Wait for the current episode action to finish.");
      }
      try {
        const started = await startEpisode(
          apiBase,
          {
            mode: "auto_research",
            browser_requested: browserRequested,
            bulletin_enabled: bulletinEnabled,
            invocation_ceiling: invocationCeiling,
            starting_instruction: startingInstruction,
            branch_name: branchName,
            // Omitted, the server turns code isolation on only where it is eligible.
            ...(codeWorktree ? {} : { code_worktree: false }),
            // Omitted, the server uses the Settings orchestrator profile.
            ...(launchConfig
              ? {
                  provider: launchConfig.provider,
                  model: launchConfig.model || null,
                  reasoning: launchConfig.reasoning,
                }
              : {}),
          },
          requestId,
        );
        replaceEpisode(started);
        replaceExactAutoResearchSelection(started.project_id, started.episode_id);
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
        return started;
      } finally {
        finishTaskStart();
        finishEpisodeAction();
      }
    },
    [
      apiBase,
      autoResearchRefusal,
      beginEpisodeAction,
      beginTaskStart,
      changeView,
      closeAutoResearchDialog,
      reload,
      replaceEpisode,
      replaceExactAutoResearchSelection,
    ],
  );
  const authorizeAutoResearch = async (
    invocationCeiling: number,
    startingInstruction: string | null,
    codeWorktree = true,
    browserRequested = false,
    launchConfig?: AgentRunConfig,
    branchName: string | null = null,
    bulletinEnabled = true,
  ) => {
    reportAutoResearchStartError(null);
    setAutoResearchBranchRefusal(null);
    try {
      await startAutoResearch(
        invocationCeiling,
        startingInstruction,
        codeWorktree,
        browserRequested,
        undefined,
        launchConfig,
        branchName,
        bulletinEnabled,
      );
    } catch (error) {
      setAutoResearchBranchRefusal(branchNameRefusal(error));
      reportAutoResearchStartError(error instanceof Error ? error.message : String(error));
    }
  };

  // The one Auto-research Stop: the run card and the WebMCP tool both call it.
  const stopAutoResearchEpisode = useCallback(
    async (episodeId: string): Promise<void> => {
      if (!apiBase) throw new Error("No RCP project is open.");
      const finishEpisodeAction = beginEpisodeAction(`stop:${episodeId}`);
      if (!finishEpisodeAction) throw new Error("Wait for the current episode action to finish.");
      const stopping = projectTasks
        .filter((task) => task.episode_id === episodeId && isActiveTask(task))
        .map((task) => ({
          task,
          mutation: beginTaskMutation(store, task.project_id, task, "stop"),
        }));
      try {
        replaceEpisode(await stopEpisode(apiBase, episodeId));
        for (const { mutation } of stopping) mutation.settle();
        scheduler.now();
      } catch (error) {
        for (const { mutation } of stopping) mutation.fail(error);
        throw error;
      } finally {
        finishEpisodeAction();
      }
    },
    [apiBase, beginEpisodeAction, projectTasks, replaceEpisode, scheduler, store],
  );
  const requestEpisodeStop = async (episodeId: string) => {
    try {
      await stopAutoResearchEpisode(episodeId);
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      throw error;
    }
  };

  const requestEpisodeArchive = async (
    requestedProjectId: string,
    episodeId: string,
    archived: boolean,
  ) => {
    const finishEpisodeAction = beginEpisodeAction(`archive:${episodeId}`);
    if (!finishEpisodeAction) throw new Error("Wait for the current episode action to finish.");
    try {
      const nextEpisode = await archiveEpisode(
        `/api/projects/${encodeURIComponent(requestedProjectId)}`,
        episodeId,
        archived,
      );
      replaceEpisode(nextEpisode);
      replaceEpisodeRunArchive(nextEpisode);
      try {
        if (isActiveProject(requestedProjectId)) {
          await Promise.all([refreshEpisodes(), refreshProjectExperimentLoops(requestedProjectId)]);
        } else {
          await refreshSpaceRuns();
        }
      } catch (error) {
        setNotice({
          kind: "error",
          text: `Run ${archived ? "archived" : "unarchived"}, but Runs could not refresh: ${error instanceof Error ? error.message : String(error)}`,
        });
      }
    } finally {
      finishEpisodeAction();
    }
  };

  const requestEpisodeContinuation = async (episodeId: string, invocationCeiling: number) => {
    if (!apiBase || episodeAction) return;
    const finishEpisodeAction = beginEpisodeAction(`continue:${episodeId}`);
    if (!finishEpisodeAction) return;
    try {
      const requestId = continuationRequestIds.current.get(episodeId) ?? crypto.randomUUID();
      continuationRequestIds.current.set(episodeId, requestId);
      const nextEpisode = await continueEpisode(apiBase, episodeId, invocationCeiling, requestId);
      continuationRequestIds.current.delete(episodeId);
      replaceEpisode(nextEpisode);
      // Continuing ends one episode and starts its successor, so an exact route
      // pinned to the predecessor follows the run instead of handing off to History.
      if (nextEpisode.mode === "auto_research") {
        replaceExactAutoResearchSelection(nextEpisode.project_id, nextEpisode.episode_id);
      } else {
        replaceExactExperimentEpisode(nextEpisode);
      }
      await reload();
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      throw error;
    } finally {
      finishEpisodeAction();
    }
  };

  const requestEpisodeMerge = async (episodeId: string, body: MergeEpisodeBody = {}) => {
    if (!apiBase || !projectId || episodeAction) return;
    const finishEpisodeAction = beginEpisodeAction(`merge:${episodeId}`);
    if (!finishEpisodeAction) return;
    const mutation = beginTaskPublication(store, projectId);
    try {
      const nextEpisode = await mergeEpisodeToMain(apiBase, episodeId, body);
      replaceEpisode(nextEpisode);
      const mergeTask = activeBranchMergeTask(nextEpisode);
      mutation.settle(mergeTask ?? undefined);
      if (mergeTask) recordStartedTask(mergeTask);
      await reload();
    } catch (error) {
      mutation.fail();
      // A pause on unfinished jobs is the panel's own prompt, not an app error.
      if (!unfinishedJobsFromError(error)) {
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
      }
      throw error;
    } finally {
      finishEpisodeAction();
    }
  };

  const messageEpisodeOrchestrator = async (
    episodeId: string,
    body: string,
    invocationCeiling?: number,
    requestId?: string,
  ) => {
    if (!apiBase || !projectId || episodeAction) return;
    const finishEpisodeAction = beginEpisodeAction(`message:${episodeId}`);
    if (!finishEpisodeAction) return;
    try {
      const saved = await sendEpisodeMessage(
        apiBase,
        episodeId,
        body,
        invocationCeiling,
        requestId,
      );
      recordEpisodeMessage(projectId, saved.episode_id, saved);
      await refreshEpisodeMessages(saved.episode_id);
      await reload();
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
    if (taskActionId || !projectId) return;
    if (action !== "pause" && mutationsDisabled && taskMayMutateGraph(task)) return;
    const finish = beginTaskAction(task.operation_id);
    if (!finish) return;
    const mutation = beginTaskMutation(store, projectId, task, action);
    const admissionKey = JSON.stringify([projectId, task.operation_id, action]);
    const requestId = taskActionRequestIds.current.get(admissionKey) ?? crypto.randomUUID();
    taskActionRequestIds.current.set(admissionKey, requestId);
    try {
      const next = await api<AgentTask>(`${apiBase}/tasks/${task.operation_id}/${action}`, {
        method: "POST",
        headers: { "Idempotency-Key": requestId },
      });
      taskActionRequestIds.current.delete(admissionKey);
      mutation.settle(next);
      if (presentTask && isActiveGraph(task.project_id)) presentAgentTask(next);
      scheduler.now();
    } catch (error) {
      if (error instanceof ApiError && error.status < 500)
        taskActionRequestIds.current.delete(admissionKey);
      mutation.fail(error);
    } finally {
      if (taskActionNeedsAuthoritativeProjectReload(task, action)) await refreshRunsAfter();
      finish();
    }
  };

  // Experiment recovery stays busy until Runs shows the server's control state,
  // so a stale control cannot offer the same recovery again.
  const refreshRunsAfter = () =>
    reload().catch((error) =>
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) }),
    );

  const operateEpisodeOrchestratorTask = async (
    task: AgentTask,
    action: "pause" | "resume" | "retry",
  ) => {
    await operateTask(task, action, false);
    await refreshEpisodes();
  };

  const repairGraphUpdate = async (
    operationId: string,
    action: GraphUpdateRecovery = "repair",
  ): Promise<void> => {
    const finishTaskRepair = beginTaskRepair(operationId);
    if (!finishTaskRepair) {
      throw new Error("Another task action is already being submitted.");
    }
    const mutation = projectId ? beginTaskPublication(store, projectId, operationId) : null;
    try {
      if (mutationsDisabled) {
        throw new Error("Graph repair is unavailable while replay is degraded.");
      }
      if (action === "apply_again") {
        const updated = await api<AgentTask>(
          `${apiBase}/tasks/${encodeURIComponent(operationId)}/apply-graph-update-again`,
          { method: "POST" },
        );
        mutation?.settle(updated);
        setNotice(null);
        // The task stays succeeded, so completion-driven refresh never fires:
        // reload the graph and chats to show the new receipt and its actions.
        await reload(false).catch((error) =>
          setNotice({
            kind: "error",
            text: `Apply again finished, but the project could not refresh: ${error instanceof Error ? error.message : String(error)}`,
          }),
        );
        return;
      }
      const next = await api<AgentTask>(
        `${apiBase}/tasks/${encodeURIComponent(operationId)}/repair-graph-update`,
        { method: "POST" },
      );
      mutation?.settle(next);
      recordStartedTask(next);
      setNotice(null);
    } catch (error) {
      mutation?.fail();
      throw error;
    } finally {
      finishTaskRepair();
    }
  };

  const retryAgentTask = async (task: AgentTask, config: AgentRunConfig) => {
    if (taskActionId || mutationsDisabled || !projectId) return;
    const finish = beginTaskAction(task.operation_id);
    if (!finish) return;
    const mutation = beginTaskMutation(store, projectId, task, "retry");
    const body = JSON.stringify(taskRetryRequestBody(task, config));
    const admissionKey = JSON.stringify([projectId, task.operation_id, "retry", body]);
    const requestId = taskActionRequestIds.current.get(admissionKey) ?? crypto.randomUUID();
    taskActionRequestIds.current.set(admissionKey, requestId);
    try {
      const next = await api<AgentTask>(`${apiBase}/tasks/${task.operation_id}/retry`, {
        method: "POST",
        headers: { "Idempotency-Key": requestId },
        body,
      });
      taskActionRequestIds.current.delete(admissionKey);
      mutation.settle(next);
      if (isActiveGraph(task.project_id)) {
        if (!isExperimentLoopRecovery(task)) presentAgentTask(next);
        closeRetryTask();
      }
      scheduler.now();
    } catch (error) {
      if (error instanceof ApiError && error.status < 500)
        taskActionRequestIds.current.delete(admissionKey);
      mutation.fail(error);
    } finally {
      if (taskActionNeedsAuthoritativeProjectReload(task, "retry")) await refreshRunsAfter();
      finish();
    }
  };

  const refreshAgentTask = useCallback(
    async (operationId: string): Promise<AgentTask> => {
      if (!projectId) throw new Error("No project is open.");
      const owner = crypto.randomUUID();
      const id = store.register(owner, taskResourceProfile(projectId, operationId));
      try {
        await store.refresh(id);
        store.invalidate(tasksResourceKey(projectId));
        return store.committedValue<AgentTask>(id)!;
      } finally {
        store.release(owner, [id]);
      }
    },
    [projectId, store],
  );

  const requestRetry = (task: AgentTask) => {
    if (task.kind === "seed" || task.kind === "refresh") {
      chooseRetryTask(task);
      return;
    }
    void operateTask(task, "retry");
  };

  const commitProjectOpen = (id: string, experimentRoute: string | null = null) => {
    if (projectId !== id) rememberProjectState(projectId);
    commitProjectRoute(id, experimentRoute);
  };
  const openProject = (id: string, experimentRoute: string | null = null) => {
    if (requestDesktopProjectOpen(id, experimentRoute)) return;
    commitProjectOpen(id, experimentRoute);
  };
  const continueDesktopProjectOpen = () => {
    continueDesktopProjectAccess(commitProjectOpen);
  };
  const projectsRef = useRef(projects);
  projectsRef.current = projects;
  const requestDesktopProjectOpenRef = useRef(requestDesktopProjectOpen);
  requestDesktopProjectOpenRef.current = requestDesktopProjectOpen;
  const commitProjectOpenRef = useRef(commitProjectOpen);
  commitProjectOpenRef.current = commitProjectOpen;
  const projectIndexWebMcpTools = useMemo(
    () =>
      projectIndexToolDefinitions(
        () => projectsRef.current,
        (id) => {
          if (requestDesktopProjectOpenRef.current(id, null)) return false;
          // Let the current tool return before switching to the project tool surface. Aborting
          // its index registration during the call makes the browser report a false stale error.
          window.setTimeout(() => commitProjectOpenRef.current(id), 0);
          return true;
        },
      ),
    [],
  );
  const {
    project: webMcpProject,
    indexAvailable: projectIndexWebMcpAvailable,
    key: webMcpSurfaceKey,
  } = webMcpSurface({ backendSessionReady, setupOpen, loading, projectId, project });
  // openNodeById and openChats change every render; rcp_open_view reads the latest at call time.
  const openNodeByIdRef = useRef(openNodeById);
  openNodeByIdRef.current = openNodeById;
  const openChatsRef = useRef(openChats);
  openChatsRef.current = openChats;
  // A flag's chat is bound to main; from a branch, switch there, then open it.
  const [pendingMainChat, setPendingMainChat] = useState<string | null>(null);
  const loadedTargetKind = project?.graph_target.kind;
  useEffect(() => {
    // The chat reads the loaded snapshot's target, so wait until main has loaded.
    if (!pendingMainChat || graphTarget.kind !== "main" || loadedTargetKind !== "main" || loading)
      return;
    setPendingMainChat(null);
    openChatsRef.current(pendingMainChat);
  }, [pendingMainChat, graphTarget, loadedTargetKind, loading]);
  const webMcpViewOwners = useMemo<WebMcpViewOwners>(
    () => ({
      openNode: (nodeId) => openNodeByIdRef.current(nodeId),
      openConversation: (transcript) => {
        selectCanonicalChat(transcript);
        openChatsRef.current(transcript.chat_id);
      },
      openRunningConversation: (chatId) => openChatsRef.current(chatId),
      // Like the exact-selection owners, replace the address in place and apply its
      // selection; the route keeps the page's graph target and fires no hashchange.
      openRunRoute: (hash) => {
        window.history.replaceState(null, "", hash);
        const route = parseProjectHash(hash);
        applyRouteSelection(
          route.view,
          route.experimentId,
          route.experimentRoute,
          route.autoResearchEpisodeId,
        );
      },
      openTab: changeView,
      openArtifact: showWebMcpArtifactViewer,
      isCurrent: (id, target) =>
        isActiveProject(id) && sameGraphTarget(activeGraphTargetRef.current, target),
    }),
    [
      applyRouteSelection,
      changeView,
      isActiveProject,
      selectCanonicalChat,
      showWebMcpArtifactViewer,
    ],
  );
  const broadReadTools = useProjectBroadReadTools(
    webMcpProject,
    backendSessionReady ? (actorIdentity?.user.user_id ?? null) : null,
    verifiedHealth?.space_id ?? null,
  );
  const webMcpTools = useMemo(() => {
    if (webMcpProject) {
      const project = webMcpProject;
      return [
        ...playbookToolDefinitions(),
        ...projectReadToolDefinitions(project, episodes),
        ...broadReadTools,
        ...providerLoginToolDefinitions(loadProviderLogins),
        ...projectArtifactToolDefinitions(
          project,
          tasks,
          episodes,
          showWebMcpArtifactViewer,
          webMcpArtifactSource,
        ),
        ...projectConversationToolDefinitions(
          project,
          visibleChatSummaries,
          visibleChatSummaries.length,
          tasks,
          webMcpConversationSource,
          taskStarting,
        ),
        ...projectConversationSendToolDefinitions(
          project,
          tasks,
          webMcpConversationSource,
          taskStarting,
          createWebMcpConversation,
          startWebMcpConversationTurn,
        ),
        ...projectExperimentToolDefinitions(
          project,
          tasks,
          watchers,
          taskStarting,
          webMcpExperimentStartProjectId === project.id,
          mutationsDisabled,
          experimentStartRequiresSync,
          startWebMcpExperiment,
        ),
        ...projectAutoResearchToolDefinitions(
          project,
          autoResearchRefusal,
          (ceiling, instruction, codeWorktree, requestId) =>
            startAutoResearch(ceiling, instruction, codeWorktree, false, requestId),
        ),
        ...projectEpisodeStopToolDefinitions(
          project,
          episodes,
          requestExperimentStop,
          stopAutoResearchEpisode,
          experimentStopIds.size > 0 || Boolean(episodeAction?.startsWith("stop:")),
        ),
        ...projectViewToolDefinitions(project, tasks, episodes, webMcpViewOwners, {
          ...webMcpArtifactSource,
          loadTranscript: loadWebMcpConversation,
          loadExperimentEntries: async () =>
            (await loadProjectExperimentEpisodes(project.id)).entries,
        }),
        // Published for voice only; webMcpHostTools keeps them off WebMCP.
        ...voiceTerminalToolDefinitions(project.id, {
          fetchJson: api,
          openSocket: openTerminalSocket,
        }),
      ];
    }
    return projectIndexWebMcpAvailable ? projectIndexWebMcpTools : [];
  }, [
    autoResearchRefusal,
    broadReadTools,
    createWebMcpConversation,
    episodeAction,
    episodes,
    experimentStartRequiresSync,
    experimentStopIds,
    loadWebMcpConversation,
    mutationsDisabled,
    projectIndexWebMcpAvailable,
    projectIndexWebMcpTools,
    requestExperimentStop,
    showWebMcpArtifactViewer,
    startAutoResearch,
    startWebMcpConversationTurn,
    startWebMcpExperiment,
    stopAutoResearchEpisode,
    taskStarting,
    tasks,
    visibleChatSummaries,
    watchers,
    webMcpArtifactSource,
    webMcpConversationSource,
    webMcpExperimentStartProjectId,
    webMcpProject,
    webMcpViewOwners,
  ]);
  // Why each conditional project tool is missing, for the catalog's resolve().
  const webMcpRefusals = useMemo(
    () =>
      webMcpProject
        ? {
            rcp_start_experiment: experimentStartRefusal(
              webMcpProject,
              taskStarting,
              mutationsDisabled,
              experimentStartRequiresSync,
            ),
            rcp_stop_episode: episodeStopRefusal(webMcpProject, episodes),
            rcp_authorize_auto_research: autoResearchRefusal,
          }
        : {},
    [
      autoResearchRefusal,
      episodes,
      experimentStartRequiresSync,
      mutationsDisabled,
      taskStarting,
      webMcpProject,
    ],
  );
  // The voice agent acts through the same surface and identity gate as WebMCP.
  const voicePageRef = useRef<VoicePageState>({
    project: null,
    tasks: [],
    conversationSource: webMcpConversationSource,
    runScope: [],
  });
  voicePageRef.current = {
    project: webMcpProject,
    tasks,
    conversationSource: webMcpConversationSource,
    runScope,
  };
  const voice = useVoiceAgent({
    ready: backendSessionReady,
    spaceId: verifiedHealth?.space_id ?? null,
    page: voicePageRef,
  });
  useEffect(() => {
    publishToolSurface(
      webMcpProject ? "project" : projectIndexWebMcpAvailable ? "project-index" : null,
      webMcpTools,
      webMcpRefusals,
    );
  }, [projectIndexWebMcpAvailable, webMcpProject, webMcpRefusals, webMcpTools]);
  // WebMCP host agents get no RCP confirmation card, so voice-only tools stay off it.
  const webMcpHostTools = useMemo(() => webMcpHostDefinitions(webMcpTools), [webMcpTools]);
  const webMcpRegistryRef = useRef<{
    surfaceKey: string;
    registry: WebMcpToolRegistry;
  } | null>(null);
  useEffect(() => {
    const current = webMcpRegistryRef.current;
    if (!webMcpSurfaceKey || webMcpHostTools.length === 0) {
      current?.registry.dispose();
      webMcpRegistryRef.current = null;
      return;
    }
    if (current?.surfaceKey === webMcpSurfaceKey) {
      current.registry.update(webMcpHostTools);
      return;
    }
    current?.registry.dispose();
    const registry = createWebMcpToolRegistry(webMcpHostTools);
    webMcpRegistryRef.current = registry ? { surfaceKey: webMcpSurfaceKey, registry } : null;
  }, [webMcpSurfaceKey, webMcpHostTools]);
  useEffect(
    () => () => {
      webMcpRegistryRef.current?.registry.dispose();
      webMcpRegistryRef.current = null;
      publishToolSurface(null, []);
    },
    [],
  );
  // Leaving a project returns to the index of the space you are in. A team
  // space keeps its own index, so Cmd+T means the same thing in both. Leaving
  // the space itself is the separate, explicit action below.
  const returnToProjects = () => {
    rememberProjectState(projectId);
    returnToProjectIndex();
  };

  const exitTeamSpace = () => {
    voice.end("space");
    void returnDesktopToPersonal().catch((error) => {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    });
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

  const deleteProject = async (id: string, stopToken: string) => {
    const result = await api<ProjectDeletionResult>(
      `/api/projects/${encodeURIComponent(id)}?stop_token=${encodeURIComponent(stopToken)}`,
      { method: "DELETE" },
    );
    removeProject(id);
    forgetProjectViewport(id);
    try {
      const key = humanDraftStorageKey(draftActorId, id);
      if (key) localStorage.removeItem(key);
    } catch {
      // The project is already deleted; a stranded draft key must not fail the action.
    }
    return result;
  };

  const activateProjectTab = (id: string) => {
    if (id === projectId) return;
    rememberProjectState(projectId);
    activateProjectRoute(id);
  };

  const closeDockedProject = (id: string) => {
    if (!closeProjectRoute(id)) return;
    setExperimentStartOverlap((current) => (current?.projectId === id ? null : current));
    forgetProjectViewport(id);
  };

  useEffect(() => {
    if (!desktop || !backendSessionReady) return;
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

  const movePersonalProjectToTeam =
    desktop &&
    authenticatedHealth?.space_kind === "personal" &&
    authenticatedHealth.project_creation.intents.some(
      (intent) => intent.intent === "move_personal_project_to_team" && intent.eligible,
    )
      ? openMoveProjectSetup
      : undefined;
  const updateHasActiveWork =
    Boolean(activeTask) ||
    (desktopUpdate?.active_agent_tasks ?? authenticatedHealth?.active_agent_tasks ?? 0) > 0;
  const applyUpdate = async () => {
    await applyDesktopShellUpdate(Boolean(activeTask), async () => {
      const identity = await reverifyIdentity("update-apply");
      if (!identity.ok) return { ok: false, activeAgentTasks: 0 };
      const details = await api<Health>("/api/health/details");
      return { ok: true, activeAgentTasks: details.active_agent_tasks };
    });
  };

  const updateSurface = (
    <UpdateNotice
      notice={releaseUpdate}
      identity={buildIdentity}
      desktop={desktop}
      updaterEnabled={updaterEnabled}
      update={desktopUpdate}
      activeWork={updateHasActiveWork}
      expanded={updateExpanded}
      applying={updateApplying}
      error={updateError}
      onExpand={expandUpdate}
      onApply={() => void applyUpdate()}
      onDismiss={dismissUpdate}
    />
  );
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
          <FolderLock size={20} aria-hidden="true" />
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
  // Reverification keeps the rendered tree under a blocking overlay. Member
  // changes still remount the outer identity boundary.
  const holdRenderedTree =
    lastProjectOpen.current !== null &&
    !!actorIdentity &&
    !!authenticatedHealth &&
    !teamSessionRequired &&
    !ownerSessionRequired;
  const reverificationSurface =
    holdRenderedTree && (identityIssue || !actorIdentityChecked) ? (
      <ReconnectOverlay
        issue={identityIssue}
        reconnecting={reconnecting}
        desktop={desktop}
        onReconnect={() => void reconnectBackend()}
      />
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
  const voiceSurface = (
    <VoicePanel
      voice={voice}
      graphRefs={graphRefs}
      onOpenSettings={() => setSpaceSettingsOpen(true)}
    />
  );
  const setupRoute: ProjectSetupRoute = setupOpen
    ? parseProjectSetupRoute(window.location.hash)
    : { kind: "none" };

  if (!identityReady && !holdRenderedTree)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>Verifying the RCP backend</span>
        {acceptanceAgentSurface}
      </div>
    );
  if (identityIssue && !holdRenderedTree)
    return (
      <div className="fatal-state reconnect-state">
        <TriangleAlert />
        <h1>Reconnect to RCP</h1>
        <p>{identityIssue}</p>
        <button
          className="button secondary"
          disabled={reconnecting}
          onClick={() => void reconnectBackend()}
        >
          {reconnecting ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}{" "}
          {backendReconnectLabel(desktop)}
        </button>
        {updateSurface}
        {acceptanceAgentSurface}
      </div>
    );
  if (!actorIdentityChecked && !holdRenderedTree)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>Verifying your identity</span>
        {acceptanceAgentSurface}
      </div>
    );
  if (ownerSessionRequired)
    return (
      <TeamLoginBoundary
        personal
        spaceName={null}
        initialCode={initialOwnerCode}
        onAuthenticate={authenticateOwnerSession}
        onPair={async () => {}}
      />
    );
  // A successful sign-in keeps the pending route; the project open that follows
  // still checks access.
  if (teamSessionRequired)
    return (
      <>
        <TeamLoginBoundary
          spaceName={
            verifiedHealth && "space_name" in verifiedHealth ? verifiedHealth.space_name : null
          }
          onAuthenticate={authenticateTeamSession}
          onPair={pairTeamDevice}
        />
        {acceptanceAgentSurface}
      </>
    );
  if (actorIdentity && new URLSearchParams(window.location.search).has("project-locator")) {
    return (
      <>
        <ProjectLocatorBoundary />
        {actorNameSurface}
        {reverificationSurface}
      </>
    );
  }
  if (loading && !project)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>{projectId ? "Opening project" : "Reading the project index"}</span>
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {reverificationSurface}
        {acceptanceAgentSurface}
        {voiceSurface}
      </div>
    );
  if (setupOpen)
    return (
      <>
        <ProjectSetup
          key={projectSetupRouteKey(setupRoute)}
          projectCreation={authenticatedHealth!.project_creation}
          spaceKind={verifiedHealth!.space_kind}
          group={
            setupRoute.kind === "create"
              ? projectGroups.find((group) => group.id === setupRoute.groupId)
              : null
          }
          onCancel={() => {
            if (setupRoute.kind === "create" && setupRoute.groupId)
              window.location.hash = projectGroupHash(setupRoute.groupId);
            else returnToProjects();
          }}
          onCreated={(id) => {
            const groupId = setupRoute.kind === "create" ? setupRoute.groupId : undefined;
            if (!groupId) return openProject(id);
            // Creation is the same in every space; the group's shelf files the project
            // with one move, where a failed move keeps its retry.
            setGroupFiling({ projectId: id, groupId });
            window.location.hash = projectGroupHash(groupId);
          }}
          setupRoute={setupRoute}
        />
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {reverificationSurface}
        {acceptanceAgentSurface}
        {voiceSurface}
      </>
    );
  if (spaceSettingsOpen)
    return (
      <>
        <SpaceSettings
          spaceKind={verifiedHealth?.space_kind ?? "personal"}
          updateNotice={releaseUpdate}
          onReleaseCheck={setReleaseUpdate}
          cacheProjectId={projectId ?? projects[0]?.id ?? null}
          cacheClearDisabled={Boolean(activeTask)}
          onAllCachesCleared={(clearedProjectId, cacheMetrics) => {
            if (project?.id !== clearedProjectId) return;
            updateProject((current) =>
              current ? { ...current, cache_metrics: cacheMetrics } : current,
            );
          }}
          onLoginChanged={() => void refreshReadiness().catch(() => {})}
          onClose={() => setSpaceSettingsOpen(false)}
        />
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {reverificationSurface}
        {acceptanceAgentSurface}
        {voiceSurface}
      </>
    );
  if (!projectId)
    return (
      <>
        <ProjectLanding
          themeChoice={appearance.theme}
          colorModeChoice={appearance.mode}
          onThemeChoiceChange={appearance.setTheme}
          onColorModeChoiceChange={appearance.setMode}
          palette={appearance.palette}
          projects={projects}
          groups={projectGroups}
          onGroupsChanged={loadProjectIndex}
          groupFiling={groupFiling}
          onGroupFilingTaken={() => setGroupFiling(null)}
          invitations={projectInvitations}
          onAnswerInvitation={answerProjectInvitation}
          spaceRuns={spaceRuns}
          onOpen={openProject}
          onOpenExperiment={openProject}
          onArchiveEpisode={requestEpisodeArchive}
          onCreate={openSetup}
          projectCreation={authenticatedHealth!.project_creation}
          onMovePersonalProjectToTeam={movePersonalProjectToTeam}
          onDelete={deleteProject}
          openProjectTabs={openProjectTabs}
          onActivateProjectTab={activateProjectTab}
          onCloseProjectTab={closeDockedProject}
          identity={actorIdentity}
          identityError={actorIdentityError}
          onRequestIdentityName={requestActorName}
          onExitTeamSpace={desktop ? exitTeamSpace : undefined}
          onOpenSpaceSettings={() => setSpaceSettingsOpen(true)}
          voiceControl={<VoiceButton voice={voice} className="landing-space-settings" />}
          textScale={desktop ? { value: textScale, onChange: changeAppTextScale } : undefined}
        />
        {notice && (
          <button className={`toast ${notice.kind}`} onClick={() => setNotice(null)}>
            {notice.text}
          </button>
        )}
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {reverificationSurface}
        {acceptanceAgentSurface}
        {voiceSurface}
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
        {reverificationSurface}
        {acceptanceAgentSurface}
        {voiceSurface}
      </div>
    );
  if (!project || !paper)
    return (
      <div className="fatal-state">
        <TriangleAlert />
        <h1>Project could not be opened</h1>
        <p>{notice?.text || "The API returned no project state."}</p>
        <button className="button secondary" onClick={returnToProjects}>
          <ArrowLeft size={16} /> All projects
        </button>
        {updateSurface}
        {desktopAccessSurface}
        {actorNameSurface}
        {reverificationSurface}
        {acceptanceAgentSurface}
        {voiceSurface}
      </div>
    );

  const openConsolidation = openConsolidationItems(consolidation);
  const needsConsolidationRenewal = consolidationNeedsRenewal(
    consolidation?.schedule ?? null,
    Date.now(),
  );
  // Flags are project-wide Inbox rows; only main's graph carries their node marks.
  const flagRows = evidenceFlagRows(
    consolidation?.evidence_flags ?? [],
    graph.nodes,
    project.repositories,
    graphTarget.kind === "main",
  );
  const flaggedNodeIds = new Set(
    graphTarget.kind === "main" ? flagRows.flatMap((row) => (row.node ? [row.node.id] : [])) : [],
  );
  const consolidationCount =
    consolidationAttentionCount(openConsolidation, needsConsolidationRenewal) + flagRows.length;
  const attentionCount =
    pendingProposals.length + attentionDecisions.length + openBlockers.length + consolidationCount;
  const showTrustFilter = view === "scientific" || view === "dag";
  const runKind = project.last_refresh_at ? "refresh" : "seed";
  const replayWarning = projectGraphMutationFailureLabel(project);
  const selectedExperimentExecution = projectExperimentExecution(
    Object.values(presentedGraph.nodes),
    tasks,
    watchers,
    presentedExperimentControl,
    selectedExperimentRoute,
    selectedTargetExperiment,
    graphTarget,
  );
  const selectedMainRouteIsStale = selectedExperimentExecution.staleMainRoute !== null;
  const selectedExperimentNode = selectedExperimentRunId
    ? selectedExperimentUsesIndex
      ? (selectedTargetExperiment?.node ?? null)
      : selectedMainRouteIsStale
        ? null
        : (presentedGraph.nodes[selectedExperimentRunId] ?? null)
    : null;
  const selectedExperimentControl = selectedExperimentRunId
    ? selectedExperimentUsesIndex
      ? (selectedTargetExperiment?.control ?? null)
      : selectedMainRouteIsStale
        ? null
        : (presentedExperimentControl[selectedExperimentRunId] ?? null)
    : null;
  const selectedExperimentNodes = Object.fromEntries(
    selectedExperimentExecution.nodes.map((node) => [node.id, node]),
  );
  const selectedExperimentConversation =
    selectedExperimentChatId && isControlNode(selectedExperimentNode?.type) ? (
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
          actorId={draftActorId}
          graphTarget={selectedExperimentChatTarget}
          node={selectedExperimentNode}
          nodes={selectedExperimentNodes}
          glossaryIndex={glossaryIndex}
          runScope={selectedExperimentControl?.operational?.session?.run_truth_scope ?? runScope}
          tasks={selectedExperimentExecution.tasks}
          watchers={selectedExperimentExecution.watchers}
          historyMessages={chatTranscripts.get(selectedExperimentChatId)?.messages}
          agentSetup={chatTranscripts.get(selectedExperimentChatId)?.agent_setup}
          chatId={selectedExperimentChatId}
          presentation="workspace"
          fixedConversation
          readOnly={selectedExperimentUsesIndex}
          graphChangesDisabled={mutationsDisabled}
          onStartTask={startAgentTask}
          onResumeTask={(task) => void operateTask(task, "resume")}
          onRetryTask={requestRetry}
          onRefreshTask={refreshAgentTask}
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

  const restoreDraftHistory = (direction: "undo" | "redo") => {
    if (!projectId || mutationsDisabled) return;
    setNotice(null);
    const { next } = direction === "undo" ? undoHumanDraft() : redoHumanDraft();
    try {
      persistProjectHumanDraft(localStorage, draftActorId, projectId, next.humanDraft, graphTarget);
    } catch (error) {
      setNotice({ kind: "error", text: errorMessage(error) });
    }
  };
  const activeExperimentIds = Object.entries(presentedExperimentControl)
    .filter(([, control]) => control.active)
    .map(([id]) => id);
  const graphEditingProps: GraphEditingProps = {
    projectId: project.id,
    mutationsDisabled,
    editOptions,
    onRetryEditOptions: editOptionsFailed
      ? () => setEditOptionsReload((count) => count + 1)
      : undefined,
    canonicalGraph: graph,
    humanDraft,
    activeExperimentIds,
    onStageNode: (node, edges) =>
      updateHumanDraft((draft) => stageCanvasNode(draft, graph, node, edges)),
    onUpdateStagedNode: (node) => updateHumanDraft((draft) => updateStagedNode(draft, node)),
    onStageEdge: (edge) => updateHumanDraft((draft) => stageEdgeAddition(draft, graph, edge)),
    onReplaceEdge: (edgeId, edge) =>
      updateHumanDraft((draft) => stageEdgeReplacement(draft, graph, edgeId, edge)),
    onRemoveEdge: (edgeId) => updateHumanDraft((draft) => stageEdgeRemoval(draft, graph, edgeId)),
    onRemoveNodes: (ids) =>
      updateHumanDraft((draft) => stageNodesRemoval(draft, graph, ids, activeExperimentIds)),
    onMergeNodes: (duplicate, canonical) =>
      updateHumanDraft((draft) =>
        stageMerge(draft, graph, duplicate, canonical, activeExperimentIds),
      ),
    onRenameNode: (id, title) =>
      updateHumanDraft((draft) => {
        const staged = draft.custom_nodes[id];
        if (staged) {
          const field = primaryField(staged.type) as keyof typeof staged;
          return updateStagedNode(draft, {
            ...staged,
            title,
            ...(staged[field] === staged.title ? { [field]: title } : {}),
          });
        }
        const editable =
          graph.nodes[id]?.standing !== "asserted" ? stageNodeEditStart(draft, graph, id) : draft;
        return stageNodeEdit(editable, graph, id, { title });
      }),
    onUndo: () => restoreDraftHistory("undo"),
    onRedo: () => restoreDraftHistory("redo"),
    canUndo,
    canRedo,
  };

  const askAboutProject = () => {
    const chatId =
      unsentConversation(conversations, "project_chat")?.chatId ??
      startConversation("project_chat", null, project.name);
    openChats(chatId);
  };
  const syncControls = (
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
          draftPreviewPending ||
          Boolean(draftPreviewConflict) ||
          ontologyDraftIsStale ||
          !project.canonical_state.reachable
        }
        title={
          draftPreviewConflict ||
          (draftPreviewPending
            ? "Preparing the staged transition preview"
            : ontologyDraftIsStale
              ? "Ontology draft base is stale"
              : undefined)
        }
        aria-label={
          syncingDraft
            ? "Syncing staged changes"
            : draftPreviewPending
              ? "Preparing staged transition preview"
              : draftPreviewConflict
                ? "Resolve the staged transition conflict before Sync"
                : ontologyDraftIsStale
                  ? `Ontology conflict, ${committableDraftCount} committable changes`
                  : behindDraftCount > 0
                    ? `Sync ${committableDraftCount} committable changes, ${behindDraftCount} behind`
                    : undefined
        }
        onClick={() => void syncHumanDraft()}
      >
        {syncingDraft || draftPreviewPending ? (
          <LoaderCircle className="spin" size={14} />
        ) : ontologyDraftIsStale || draftPreviewConflict ? (
          <TriangleAlert size={14} />
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
  );
  const autoResearchButton = (
    <button
      className="button secondary auto-research-control"
      disabled={autoResearchRefusal !== null && continueBranch === null}
      aria-label="Auto-research"
      title={continueBranch ? undefined : (autoResearchRefusal ?? undefined)}
      onClick={() => {
        setAutoResearchBranchRefusal(null);
        openAutoResearchDialog();
      }}
    >
      <Telescope size={14} /> <span className="auto-research-label">Auto-research</span>
    </button>
  );
  const projectUtilities = (
    <>
      <button
        className="icon-button task-history-control"
        aria-label={activeTask ? "Project history, task in progress" : "Project history"}
        onClick={openProjectHistory}
      >
        <History size={16} />
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
        <RefreshCw className={activeTask && !activeTask.pausing ? "spin" : ""} size={16} />
      </button>
      <VoiceButton voice={voice} className="icon-button" />
      <button
        className="icon-button space-settings-control"
        aria-label="Space settings"
        title="Space settings"
        onClick={() => setSpaceSettingsOpen(true)}
      >
        <Settings size={16} />
      </button>
      <LandingIdentityMenu
        compact={!phone}
        identity={actorIdentity}
        identityError={actorIdentityError}
        onRequestName={requestActorName}
        appearance={{
          themeChoice: appearance.theme,
          colorModeChoice: appearance.mode,
          onThemeChoiceChange: appearance.setTheme,
          onColorModeChoiceChange: appearance.setMode,
        }}
        textScale={desktop ? { value: textScale, onChange: changeAppTextScale } : undefined}
      />
    </>
  );
  const selectGraphRef = (target: GraphTargetRef) => {
    if (sameGraphTarget(target, graphTarget)) return;
    // Nodes are not in the URL: the open effect keeps one only if the new ref has it.
    const next = selectionAfterGraphSwitch(
      {
        view,
        nodeId: null,
        // The board keeps a remembered chat; only an open one follows the switch.
        chatId: view === "chats" && !agentsBoard ? selectedChatId : null,
        episodeId: selectedAutoResearchEpisodeId,
      },
      {
        nodeIds: new Set(),
        chatIds: new Set(
          inventoryConversations
            .filter((conversation) => sameGraphTarget(conversation.graphTarget, target))
            .map((conversation) => conversation.chatId),
        ),
        episodeIds: new Set(
          episodes
            .filter((episode) => sameGraphTarget(episode.graph_target, target))
            .map((episode) => episode.episode_id),
        ),
        views: GRAPH_REF_VIEWS,
      },
    );
    window.location.hash = projectViewHash(project.id, target, next.view, {
      ...(next.view === "chats" && next.chatId ? { chatId: next.chatId } : {}),
      ...(next.view === "execution" && next.episodeId
        ? { autoResearchEpisodeId: next.episodeId }
        : {}),
    });
  };
  const forkGraph = async (
    target: GraphTargetRef,
    revision: number,
    requestId: string,
    name?: string,
  ) => {
    const ref = await createGraphFork(project.id, target, revision, requestId, name);
    store.invalidate(resourceKeyId(refsProfile(project.id).key));
    if (ref.kind === "branch") selectGraphRef({ kind: "branch", branch_id: ref.branch_id });
  };
  const renderNavItem = (item: (typeof navItems)[number]) =>
    item.view === "terminals" ? (
      <TerminalTab
        key={`terminals-${project.id}`}
        projectId={project.id}
        refreshKey={JSON.stringify([project.machines, project.repositories])}
        active={view === "terminals"}
        onClick={() => changeView("terminals")}
        onError={reportErrorNotice}
      />
    ) : (
      <button
        key={item.view}
        className={navItemActive(item.view, view) ? "active" : ""}
        aria-current={navItemActive(item.view, view) ? "page" : undefined}
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
        {item.view === "attention" && attentionCount > 0 && (
          <small className="inbox-count">{attentionCount}</small>
        )}
        {item.view === "artifacts" && paper.sync_state !== "synced" && <small>1</small>}
        {item.view === "chats" && chatsIndicator && (
          <small
            className={`chats-indicator ${chatsIndicator}`}
            aria-label={chatsIndicator === "active" ? "Chat task active" : "Unread chat result"}
          >
            {chatsIndicator === "active" ? "•" : unreadChatIds.size}
          </small>
        )}
      </button>
    );
  const branchPicker = (
    <>
      {graphView.stale && (
        <span aria-label="Graph may be stale" style={{ outline: "1px solid var(--danger, #c44)" }}>
          <button
            className="button icon"
            aria-label="Retry graph"
            title="Retry graph"
            onClick={() =>
              store.invalidate(resourceKeyId(graphResourceKey(project.id, graphTarget)))
            }
          >
            ↻
          </button>
        </span>
      )}
      {tasksUnavailable && (
        <span aria-label="Tasks may be stale" style={{ outline: "1px solid var(--danger, #c44)" }}>
          <button
            className="button icon"
            aria-label="Retry tasks"
            title="Retry tasks"
            onClick={retryTasks}
          >
            ↻
          </button>
        </span>
      )}
      <GraphPicker
        refs={projectGraphRefs}
        unavailable={graphRefs?.projectId === project.id && graphRefs.stale === true}
        activeRef={graphTarget}
        projectId={project.id}
        compact={phone}
        onSelect={selectGraphRef}
        viewedHead={project.graph_head}
        onCreate={(name, requestId, head) => forkGraph(head.target, head.revision, requestId, name)}
        createDisabled={mutationsDisabled}
        onOpen={() => setGraphRefsReread(null)}
        onRefUpdated={(updated) => {
          setGraphRefsReread(null);
          setGraphRefs((current) =>
            current?.projectId === project.id
              ? {
                  ...current,
                  refs:
                    current.refs?.map((ref) =>
                      ref.kind === "branch" && ref.branch_id === updated.branch_id ? updated : ref,
                    ) ?? null,
                }
              : current,
          );
        }}
      />
    </>
  );
  const viewChoice = {
    layout: (view === "dag" ? "dag" : "scientific") as ResearchLayout,
    trustView,
    onLayout: changeView,
    onTrustView: changeTrustView,
  };
  const phonePrimaryViews = new Set<AppView>(["overview", "attention", "scientific", "execution"]);
  const projectDock = (
    <ProjectDock
      sync={refreshHealth.sync}
      tabs={openProjectTabs}
      activeProjectId={projectId}
      onActivate={activateProjectTab}
      onClose={closeDockedProject}
    />
  );

  // Pinned pages follow Files | Paper in both Artifacts sections.
  const pinTabs = (
    <>
      {pins.map((pin) => (
        <PinnedViewTab
          key={`${pinScope}:${pin.artifact_id}`}
          projectId={project.id}
          branchId={pinBranchId}
          pin={pin}
          active={activePin?.artifact_id === pin.artifact_id}
          onSelect={() => {
            setPinSelection({ scope: pinScope, id: pin.artifact_id });
            setRequestedPin(null);
            changeView("artifacts");
          }}
        />
      ))}
      {Boolean(pinsResource.error) && (
        <button
          aria-label="Retry pinned views"
          title={errorMessage(pinsResource.error)}
          onClick={() => pinsProfile && store.invalidate(resourceKeyId(pinsProfile.key))}
        >
          <RefreshCw size={14} />
        </button>
      )}
    </>
  );
  return createElement(
    EvidenceFlagProvider,
    {
      nodeIds: flaggedNodeIds,
      onOpenInbox: () => {
        clearNodeSelections();
        changeView("attention");
      },
    },
    <div className="app-shell overview-shell">
      {acceptanceAgentSurface}
      {voiceSurface}
      {phone && (
        <PhoneProjectBar
          projectName={project.name}
          branchPicker={branchPicker}
          hasDraft={draftChangeCount > 0 || behindDraftCount > 0}
          onBack={returnToProjects}
          dock={projectDock}
          sync={syncControls}
          menu={
            <>
              {showTrustFilter && <ViewPicker inline {...viewChoice} />}
              {autoResearchButton}
              {projectUtilities}
            </>
          }
        />
      )}
      {!phone && !projectHeaderCollapsed && (
        <header className={`project-header${draftChangeCount > 0 ? " has-draft" : ""}`}>
          <div className="project-header-navigation">
            <button className="project-back" onClick={returnToProjects} aria-label="All projects">
              <ArrowLeft size={16} />
            </button>
            {projectDock}
          </div>
          <div className="project-header-actions" id="project-header-actions">
            <div
              className="project-header-group project-action-group"
              role="group"
              aria-label="Project actions"
            >
              {syncControls}
              <button
                className="button secondary"
                disabled={projectReconciliation !== "authoritative"}
                onClick={askAboutProject}
              >
                <MessageCircle size={14} /> Ask
              </button>
              {autoResearchButton}
            </div>
            <div
              className="project-header-group project-utility-group"
              role="group"
              aria-label="Project utilities"
            >
              {projectUtilities}
            </div>
          </div>
        </header>
      )}

      {!phone && (
        <nav className="project-tabs" aria-label="Project panels">
          {projectHeaderCollapsed && (
            <button
              className="project-tabs-back project-back"
              onClick={returnToProjects}
              aria-label="All projects"
            >
              <ArrowLeft size={16} />
            </button>
          )}
          {/* Folded, the expand control sits beside the back arrow it came from. */}
          <button
            aria-expanded={!projectHeaderCollapsed}
            aria-controls={!projectHeaderCollapsed ? "project-header-actions" : undefined}
            aria-label={
              projectHeaderCollapsed ? "Expand project header" : "Collapse project header"
            }
            className="project-tabs-toggle"
            title={projectHeaderCollapsed ? "Expand project header" : "Collapse project header"}
            onClick={toggleProjectHeader}
          >
            {projectHeaderCollapsed ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
          </button>
          {projectHeaderCollapsed && (
            <ProjectDock
              className="project-tabs-project-dock"
              tabs={openProjectTabs}
              activeProjectId={projectId}
              onActivate={activateProjectTab}
              onClose={closeDockedProject}
            />
          )}
          {navItems.map(renderNavItem)}
          {/* Pinned to the strip's right edge so a long tab row scrolls under it. */}
          <div className="project-tabs-trailing">
            {showTrustFilter && <ViewPicker {...viewChoice} />}
            {branchPicker}
          </div>
        </nav>
      )}

      {(projectReconciliation === "opening" || projectReconciliation === "reconciling") && (
        <div className="branch-switch-progress" role="progressbar" aria-label="Loading branch" />
      )}
      {(dockedNodes.length > 0 || dockedArtifact) && (
        <section className="node-window-dock" aria-label="Docked windows">
          <div className="node-window-dock-label">
            <Network size={14} />
            <span>Docked</span>
          </div>
          <div className="node-window-dock-items">
            {dockedArtifact && (
              <button
                className="node-window-dock-item"
                type="button"
                aria-label={`Restore ${dockedArtifact.title}`}
                onClick={dockedArtifact.restore}
              >
                <FileText size={14} />
                <span>{dockedArtifact.title}</span>
              </button>
            )}
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
        {draftPreviewPending && (
          <div className="coverage-banner" role="status">
            <LoaderCircle className="spin" size={16} aria-hidden="true" />
            <span>
              <strong>Preparing staged transition preview.</strong>
            </span>
          </div>
        )}
        {draftPreviewConflict && (
          <div className="coverage-banner validation-rejected" role="alert">
            <TriangleAlert size={16} aria-hidden="true" />
            <span>
              <strong>Staged transition conflict.</strong> {draftPreviewConflict} Your staged input
              is kept;{" "}
              {draftTransitionProjection
                ? "the graph remains at the last valid staged projection."
                : "the graph remains at canonical state."}
            </span>
          </div>
        )}
        {!draftPreviewPending &&
          !draftPreviewConflict &&
          draftTransitionProjection &&
          draftTransitionProjection.head.revision !== graph.revision && (
            <div className="coverage-banner" role="status">
              <GitBranch size={16} aria-hidden="true" />
              {phone ? (
                <span title={`Canonical state remains revision ${graph.revision} until Sync.`}>
                  Staged preview · revision {draftTransitionProjection.head.revision}
                </span>
              ) : (
                <span>
                  <strong>Staged transition preview.</strong> Candidate revision{" "}
                  {draftTransitionProjection.head.revision}; canonical state remains revision{" "}
                  {graph.revision} until Sync.
                </span>
              )}
            </div>
          )}
        {episodeRefreshError && (
          <button
            className="button icon"
            aria-label="Retry episode refresh"
            title={episodeRefreshError}
            style={{ outline: "1px solid var(--danger, #c44)" }}
            onClick={() => void refreshEpisodes()}
          >
            ↻
          </button>
        )}
        {!project.canonical_state.reachable && (
          <div className="coverage-banner state-offline">
            <TriangleAlert size={16} />
            <span>
              <strong>Canonical state is offline.</strong> Sync is unavailable.
            </span>
          </div>
        )}
        {replayWarning && (
          <div className="coverage-banner replay-degraded" role="alert">
            <TriangleAlert size={16} />
            <span>
              <strong>Replay degraded.</strong> {replayWarning}
            </span>
          </div>
        )}
        {rejectedPatches.length > 0 && view === "attention" && (
          <div className="coverage-banner validation-rejected" role="status">
            <TriangleAlert size={16} />
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
          {view === "overview" && (
            <ProjectOverview
              project={projectWithTransitionProjection(
                project,
                presentedGraph,
                presentedExperimentControl,
                presentedAttention,
                presentedTransitionProjection?.primary_question ?? project.primary_question ?? null,
                presentedTransitionProjection?.counts ?? project.counts,
              )}
              graph={presentedGraph}
              pendingProposals={pendingProposals}
              decisionsAwaitingChoice={attentionDecisions}
              latestRevisionSummary={
                latestRevisionSummary?.to_revision === graph.revision ? latestRevisionSummary : null
              }
              onNavigate={changeView}
              digest={{
                digest: projectDigest,
                targetName: branchName(projectGraphRefs, graphTarget),
                error: projectDigestError,
                catchingUp: projectDigestCatchingUp,
                onCatchUp:
                  graphTarget.kind === "main" ? () => void catchUpProjectDigest() : undefined,
                onOpenArtifact: (artifactId) => openArtifact({ projectId: project.id, artifactId }),
              }}
            />
          )}
          {view === "attention" && (
            <div className="attention-page">
              <div className="attention-main">
                <AttentionOverview
                  proposals={pendingProposals}
                  decisions={attentionDecisions}
                  blockers={openBlockers}
                  consolidationCount={consolidationCount}
                  onSelectNode={openNode}
                />
                <ProposalJudgmentSection
                  proposals={pendingProposals}
                  graph={attentionGraph}
                  proposalActions={presentedAttention.proposal_actions}
                  staleProposalIds={presentedAttention.stale_proposal_ids}
                  refreshableProposalIds={
                    graphTarget.kind === "main" ? canonicalAttention.refreshable_proposal_ids : []
                  }
                  onRefresh={async (proposal) =>
                    (await refreshProposal(apiBase, proposal.id)).chat_id
                  }
                  onOpenChat={openChats}
                  onOpenNode={openNodeById}
                  glossaryIndex={glossaryIndex}
                  draft={mutationsDisabled ? null : humanDraft}
                  mutationsDisabled={mutationsDisabled}
                  onDecision={(proposal, decision) =>
                    updateHumanDraft((draft) =>
                      stageProposalDecision(draft, graph, proposal.id, decision),
                    )
                  }
                />
                <EvidenceFlagInbox
                  key={project.id}
                  apiBase={apiBase}
                  projectId={project.id}
                  rows={flagRows}
                  writesDisabled={!consolidation?.can_write}
                  onChanged={updateEvidenceFlag}
                  onOpenNode={openNode}
                  onOpenChat={(chatId) => {
                    if (graphTarget.kind === "main") return openChats(chatId);
                    setPendingMainChat(chatId);
                    window.location.hash = projectViewHash(project.id, MAIN_GRAPH, "chats");
                  }}
                />
                <ConsolidationInbox
                  items={openConsolidation}
                  schedule={consolidation?.schedule ?? null}
                  needsRenewal={needsConsolidationRenewal}
                  error={consolidationError}
                  writesDisabled={!consolidation?.can_write}
                  onOpenReport={(artifactId) => openArtifact({ projectId: project.id, artifactId })}
                  onResolve={async (runId, action) => {
                    await resolveConsolidationRun(apiBase, runId, action);
                    refreshConsolidation();
                  }}
                  onOpenSettings={() => {
                    changeView("settings");
                    window.requestAnimationFrame(() =>
                      document
                        .getElementById(CONSOLIDATION_SETTINGS_ANCHOR)
                        ?.scrollIntoView({ block: "center" }),
                    );
                  }}
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
            <ResearchDrilldown
              graph={presentedGraph}
              trustView={trustView}
              changedNodeIds={digestChangedNodes}
              onSelectNode={openNode}
              trailRef={activeResearchTrailRef!}
            />
          )}
          {view === "dag" && (
            <DagView
              key={graphSessionKey(project.id, graphTarget)}
              graphTarget={graphTarget}
              branchChanges={project.graph_changes}
              graphRefs={projectGraphRefs}
              mergePaths={branchMergePaths}
              onInspectTask={selectTaskInspector}
              {...graphEditingProps}
              graph={presentedGraph}
              trustView={trustView}
              viewportRef={activeDagViewportRef!}
              relationFocusNodeId={dagRelationFocusId}
              onClearRelationFocus={clearDagRelationFocus}
              changedNodeIds={digestChangedNodes}
              onSelectNode={openNode}
            />
          )}
          {view === "artifacts" && (
            <div className="artifacts-shell">
              <ArtifactsSubnav
                view={view}
                filesActive={!activePin}
                paperUnsynced={paper.sync_state !== "synced"}
                failed={Boolean(pinsResource.error)}
                onChange={(next) => {
                  setPinSelection(null);
                  setRequestedPin(null);
                  changeView(next);
                }}
              >
                {pinTabs}
              </ArtifactsSubnav>
              {activePin ? (
                <PinnedView
                  key={`${pinScope}:${activePin.artifact_id}`}
                  projectId={project.id}
                  branchId={pinBranchId}
                  pin={activePin}
                  nodes={presentedGraph.nodes}
                  ready={
                    projectReconciliation === "authoritative" &&
                    project.id === projectId &&
                    sameGraphTarget(project.graph_target, graphTarget)
                  }
                  onOpenNode={openNodeById}
                />
              ) : (
                <Artifacts
                  graphRefs={projectGraphRefs ?? null}
                  key={project.id}
                  projectId={project.id}
                  nodeTitle={(nodeId) => presentedGraph.nodes[nodeId]?.title ?? null}
                  onOpenNode={openNodeById}
                />
              )}
            </div>
          )}
          {view === "terminals" && <Terminals key={project.id} projectId={project.id} />}
          {view === "execution" && (
            <div className="combined-runs-view">
              {experimentStartOverlap?.projectId === project.id && (
                <ExperimentStartOverlap
                  projectId={project.id}
                  graphRefs={projectGraphRefs ?? null}
                  loops={experimentStartOverlap.loops}
                  onDismiss={() => setExperimentStartOverlap(null)}
                />
              )}
              <ExecutionView
                graphTarget={graphTarget}
                graphRefs={projectGraphRefs ?? null}
                providerLogins={runsProviderLogins}
                onProviderLoginVerified={() => void refreshProviderLogins()}
                machines={project.machines}
                computeApiBase={apiBase}
                onOpenSettings={() => changeView("settings")}
                graph={presentedGraph}
                episodes={episodes}
                episodeAction={episodeAction}
                tasks={projectTasks}
                watchers={watchers}
                experimentControl={presentedExperimentControl}
                experimentEntriesLoaded={experimentLoopsLoaded}
                experimentUnavailableLoops={experimentLoopsUnavailableRows}
                experimentEntries={experimentLoops.filter(
                  (entry) => entry.project_id === project.id,
                )}
                exactExperimentRoute={selectedExperimentRoute}
                exactExperimentEntry={selectedTargetExperiment}
                selectedExperimentId={selectedExperimentRunId}
                focusExperimentId={focusExperimentRunId}
                selectedAutoResearchEpisodeId={selectedAutoResearchEpisodeId}
                runBusy={taskStarting}
                stopBusyIds={experimentStopIds}
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
                experimentStartsDisabled={experimentStartRequiresSync}
                onInspectTask={selectTaskInspector}
                onStopEpisode={requestEpisodeStop}
                onArchiveEpisode={requestEpisodeArchive}
                onMergeEpisode={requestEpisodeMerge}
                onContinueEpisode={requestEpisodeContinuation}
                onSendEpisodeMessage={messageEpisodeOrchestrator}
                onOperateEpisodeTask={operateEpisodeOrchestratorTask}
                onSwitchEpisodeProvider={chooseRetryTask}
                onSelectExperiment={selectExperiment}
                onOpenExperimentEntry={(entry) =>
                  commitProjectOpen(project.id, experimentBoardRouteToken(entry))
                }
                onDetailFocused={clearExperimentFocus}
                onOpenHistory={openProjectHistory}
                project={project}
                onRunExperiment={(
                  node,
                  invocationCeiling,
                  browserRequested,
                  launchConfig,
                  message,
                ) =>
                  void runExperiment(
                    node,
                    invocationCeiling,
                    undefined,
                    browserRequested,
                    launchConfig,
                    message,
                  )
                }
                onStopExperiment={(nodeId, episodeId) => void stopExperimentLoop(nodeId, episodeId)}
                onCheckExperimentWatcher={(watcherId) => void checkExperimentWatcher(watcherId)}
                onStopExperimentWatcher={(watcherId) => void stopWatcher(watcherId)}
                onRecoverExperiment={(task, action) => void operateTask(task, action, false)}
                onSwitchExperimentProvider={chooseRetryTask}
                episodeReportHref={(episodeId) => episodeReportPreviewUrl(project.id, episodeId)}
              />
            </div>
          )}
          {view === "paper" && (
            <div className="artifacts-shell">
              <ArtifactsSubnav
                view={view}
                paperUnsynced={paper.sync_state !== "synced"}
                failed={Boolean(pinsResource.error)}
                onChange={changeView}
              >
                {pinTabs}
              </ArtifactsSubnav>
              <LoadedPaperWorkspace
                graphTarget={graphTarget}
                key={project.id}
                apiBase={apiBase}
                project={project}
                tasks={projectTasks}
                onStartTask={startAgentTask}
                editorState={paperEditorStates.current.get(project.id)}
                onEditorStateChange={(state) => paperEditorStates.current.set(project.id, state)}
              />
            </div>
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
              spaceKind={verifiedHealth?.space_kind ?? "personal"}
              onRefreshReadiness={refreshReadiness}
              readinessRequest={{
                pending: settingsResource.loading,
                providerError: settingsResource.error ? String(settingsResource.error) : null,
                computeError: settingsResource.error ? String(settingsResource.error) : null,
              }}
              onMovePersonalProjectToTeam={movePersonalProjectToTeam}
              consolidation={consolidation}
              consolidationError={consolidationError}
              onConsolidationChanged={refreshConsolidation}
              onCacheMetricsChange={(cacheMetrics) => {
                updateProject((current) =>
                  current ? { ...current, cache_metrics: cacheMetrics } : current,
                );
              }}
              onSaved={(saved, retention = RETAIN_ALL_PROJECT_READINESS) => {
                store.invalidate(resourceKeyId(settingsProfile(saved.id).key));
                beginProjectSnapshotRequest(saved.id);
                if (graphTarget.kind === "main")
                  updateProject((current) =>
                    projectSettingsSavedProject(saved, current, retention),
                  );
                else void reload();
                // The saved snapshot carries the new default; the session may still
                // hold the pre-save branch snapshot while its reload is in flight.
                replaceRunScope(saved.default_run_truth_scope);
                setNotice({ kind: "info", text: "Project defaults synced." });
              }}
            />
          )}
          {view === "chats" && (
            <ChatsWorkspace
              project={project}
              actorId={draftActorId}
              conversations={inventoryConversations}
              selectedChatId={selectedChatId}
              board={agentsBoard}
              onBoardChange={setAgentsBoard}
              nodes={presentedGraph.nodes}
              graphTarget={graphTarget}
              glossaryIndex={glossaryIndex}
              runScope={runScope}
              tasks={tasks}
              watchers={watchers}
              graphChangesDisabled={mutationsDisabled}
              unreadChatIds={unreadChatIds}
              chatTranscripts={chatTranscripts}
              onSelect={(chatId) => {
                const conversation = inventoryConversations.find((item) => item.chatId === chatId);
                // An orchestrator opens in place from main; other chats open on their own graph.
                if (
                  conversation &&
                  conversation.conversationKind !== "auto_research" &&
                  !sameGraphTarget(conversation.graphTarget, graphTarget)
                ) {
                  window.location.hash = conversationHref(project.id, conversation);
                  return;
                }
                selectListedConversation(chatId, conversation?.graphTarget);
              }}
              onOpenNode={openNodeById}
              onStartTask={startAgentTask}
              onResumeTask={(task) => void operateTask(task, "resume")}
              onRetryTask={requestRetry}
              onRefreshTask={refreshAgentTask}
              onInspectTask={selectTaskInspector}
              onOpenInbox={() => changeView("attention")}
              onRepairGraphUpdate={repairGraphUpdate}
              onStopWatcher={(watcherId) => void stopWatcher(watcherId)}
              onRemoveDraft={discardDraft}
              orchestratorEpisodes={orchestratorEpisodes}
              episodeAction={episodeAction}
              onSendOrchestratorMessage={messageEpisodeOrchestrator}
              archivedRevision={chatReads?.archived.join("\n")}
              onArchiveChange={() =>
                void refreshChatReads().catch((error) =>
                  reportErrorNotice(
                    `Unread chats could not be refreshed: ${error instanceof Error ? error.message : String(error)}`,
                  ),
                )
              }
              onNewSession={(conversation) => {
                const node = conversation.nodeId
                  ? (presentedGraph.nodes[conversation.nodeId] ?? null)
                  : null;
                selectChat(
                  unsentConversation(conversations, conversation.kind, conversation.nodeId)
                    ?.chatId ?? startConversation(conversation.kind, node, project.name),
                );
              }}
            />
          )}
        </Suspense>
      </main>

      {phone && view !== "chats" && (
        <PhoneAskButton
          disabled={projectReconciliation !== "authoritative"}
          onAsk={askAboutProject}
        />
      )}
      {phone && (
        <PhoneTabBar
          primary={navItems.filter((item) => phonePrimaryViews.has(item.view)).map(renderNavItem)}
          more={navItems.filter((item) => !phonePrimaryViews.has(item.view)).map(renderNavItem)}
          moreActive={navItems.some(
            (item) => !phonePrimaryViews.has(item.view) && navItemActive(item.view, view),
          )}
          moreBadge={
            (chatsIndicator || paper.sync_state !== "synced") && (
              <small className="phone-more-badge" aria-label="Updates in More" />
            )
          }
        />
      )}

      {(
        [
          { slot: "original" as const, selected: selectedNode },
          { slot: "companion" as const, selected: companionNode },
        ] satisfies Array<{ slot: DetailWindowSlot; selected: GraphNode | null }>
      ).map(({ slot, selected }) => {
        if (!selected) return null;
        const node = presentedGraph.nodes[selected.id] ?? selected;
        const historical =
          !presentedGraph.nodes[node.id] &&
          project.graph_changes?.nodes.some(
            (change) => change.node_id === node.id && change.change === "removed",
          ) === true;
        const experimentControl = historical ? null : experimentControlForNode(node);
        return (
          <DetailDrawer
            key={`${slot}:${node.id}`}
            experimentBranchNameRefusal={
              experimentBranchRefusal?.nodeId === node.id ? experimentBranchRefusal.refusal : null
            }
            graphRefs={projectGraphRefs ?? null}
            node={node}
            projectId={project.id}
            graphTarget={graphTarget}
            historical={historical}
            branchChange={project.graph_changes?.nodes.find((change) => change.node_id === node.id)}
            mergePaths={branchMergePaths?.filter(
              (path) => path.entity === "node" && path.id === node.id,
            )}
            onInspectTask={selectTaskInspector}
            edges={Object.values(presentedGraph.edges)}
            allNodes={presentedGraph.nodes}
            glossaryIndex={glossaryIndex}
            beliefTransitions={graph.belief_transitions}
            validationMessages={graph.validation_messages}
            ontology={presentedGraph.ontology}
            priorChoiceOffBallot={presentedAttention.decision_prior_choices[node.id] ?? null}
            sizeStorageKey={nodeDetailSizeStorageKey(graphSessionKey(project.id, graphTarget))}
            detailSlot={slot}
            focusRequestToken={detailFocusTokens[slot]}
            mutationsDisabled={mutationsDisabled}
            stagedNewNode={Boolean(humanDraft?.custom_nodes[node.id])}
            onUpdateStagedNode={graphEditingProps.onUpdateStagedNode}
            stagedForRemoval={Boolean(humanDraft?.removed_node_ids.includes(node.id))}
            hasStagedNodeChange={Boolean(humanDraft?.nodes[node.id])}
            draftNodeChange={humanDraft?.nodes[node.id]}
            canonicalNode={graph.nodes[node.id]}
            behind={draftNodeIsBehind(humanDraft?.nodes[node.id], graph.nodes[node.id])}
            canonicalStanding={graph.nodes[node.id]?.standing ?? node.standing}
            experimentControl={experimentControl}
            experimentEntries={experimentLoops}
            experimentUnavailableLoops={experimentLoopsUnavailableRows}
            experimentEntriesStatus={
              // A failed latest request outranks an earlier successful load.
              experimentLoopsUnavailable
                ? "unavailable"
                : experimentLoopsLoaded
                  ? "loaded"
                  : "loading"
            }
            experimentRunDisabled={experimentStartRequiresSync}
            experimentRunBusy={taskStarting}
            decisionChoiceStaged={Boolean(
              humanDraft?.nodes[node.id]?.changes.selected_option !== undefined ||
              humanDraft?.nodes[node.id]?.changes.status === "decided",
            )}
            onUnstage={() => {
              updateHumanDraft((draft) => unstageCustomNode(draft, graph, node.id));
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
              updateHumanDraft((draft) => stageNodeEditStart(draft, graph, node.id), {
                phase: "start",
                key: node.id,
              })
            }
            onStanding={(standing, amend) =>
              updateHumanDraft(
                (draft) => stageNodeStanding(draft, graph, node.id, standing),
                amend ? { phase: "finish", key: node.id } : undefined,
              )
            }
            onStage={(changes, amend) =>
              updateHumanDraft(
                (draft) => stageNodeEdit(draft, graph, node.id, changes),
                amend ? { phase: "finish", key: node.id } : undefined,
              )
            }
            onApplyField={(changes, fieldKey) =>
              updateHumanDraft((draft) => stageNodeEdit(draft, graph, node.id, changes, [fieldKey]))
            }
            onDecisionChoice={(selectedOption) =>
              updateHumanDraft((draft) =>
                stageDecisionChoice(draft, graph, node.id, selectedOption),
              )
            }
            project={project}
            onRunExperiment={(isolation, browserRequested, launchConfig, message) =>
              void runExperiment(
                node,
                undefined,
                isolation,
                browserRequested,
                launchConfig,
                message,
              )
            }
            onResumeExperiment={(episodeId, turns) =>
              void requestEpisodeContinuation(episodeId, turns).catch(() => undefined)
            }
            inheritedIsolation={
              activeBranchEpisode
                ? { graph_isolation: true, code_worktree: activeBranchEpisode.code_worktree }
                : null
            }
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
              actorId={draftActorId}
              graphTarget={project.graph_target}
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
              agentSetup={chatTranscripts.get(floatingChat.chatId)?.agent_setup}
              chatId={floatingChat.chatId}
              presentation="floating"
              graphChangesDisabled={mutationsDisabled}
              onStartTask={startAgentTask}
              onResumeTask={(task) => void operateTask(task, "resume")}
              onRetryTask={requestRetry}
              onRefreshTask={refreshAgentTask}
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
                const chatId =
                  unsentConversation(conversations, "node_chat", floatingChat.nodeId)?.chatId ??
                  startConversation("node_chat", node, project.name);
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
        // Continue's handlers skip silently while any episode action runs, so the
        // dialog waits rather than closing over a message that was never sent.
        busy={continueBranch !== null ? episodeAction !== null : episodeAction === "start"}
        error={autoResearchStartError}
        initialInvocationCeiling={project.default_auto_research_invocation_ceiling}
        project={project}
        onClose={closeAutoResearchDialog}
        onAuthorize={(
          invocationCeiling,
          startingInstruction,
          codeWorktree,
          browserRequested,
          launchConfig,
          branchName,
          bulletinEnabled,
        ) =>
          void authorizeAutoResearch(
            invocationCeiling,
            startingInstruction,
            codeWorktree,
            browserRequested,
            launchConfig,
            branchName,
            bulletinEnabled,
          )
        }
        branchNameRefusal={autoResearchBranchRefusal}
        branchNamesInUse={branchNamesInUse}
        otherLiveRuns={liveAutoResearchEpisodes}
        graphRefs={projectGraphRefs ?? null}
        continueBranch={continueBranch}
        onContinue={requestEpisodeContinuation}
        onSendMessage={messageEpisodeOrchestrator}
      />
      {retryTask && retryConfig && (
        <RunDialog
          open
          mode="retry"
          kind={retryProfileKind(retryTask)}
          project={project}
          initialScope={retryTask.request.run_truth_scope || project.default_run_truth_scope}
          initialConfig={retryConfig}
          runOnLocked={retryTask.episode_id !== null}
          busy={taskActionId === retryTask.operation_id}
          onClose={closeRetryTask}
          onRun={(config) => void retryAgentTask(retryTask, config)}
        />
      )}
      {projectHistoryOpen && (
        <ProjectHistoryDrawer
          reportError={reportErrorNotice}
          key={graphSessionKey(project.id, graphTarget)}
          projectId={project.id}
          onFork={async (revision, requestId) => {
            await forkGraph(graphTarget, revision, requestId);
            closeProjectHistory();
          }}
          forkDisabled={mutationsDisabled}
          summaries={historyRevisionSummaries}
          tasks={projectTasks}
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
          tasks={projectTasks}
          task={inspectedTask}
          loading={taskInspectorLoading}
          actionBusy={Boolean(
            taskActionId || (activeTask && activeTask.operation_id !== taskInspectorId),
          )}
          mutatingActionsDisabled={Boolean(
            mutationsDisabled && inspectedTask && taskMayMutateGraph(inspectedTask),
          )}
          onSelect={selectTaskInspector}
          onStop={() => {
            if (inspectedTask?.episode_id) void requestEpisodeStop(inspectedTask.episode_id);
          }}
          onPause={() => inspectedTask && void operateTask(inspectedTask, "pause")}
          onResume={() => inspectedTask && void operateTask(inspectedTask, "resume")}
          onRetry={() => inspectedTask && requestRetry(inspectedTask)}
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
      {reverificationSurface}
    </div>,
  );
}

function readTextScale(): number {
  try {
    return normalizeTextScale(localStorage.getItem(TEXT_SCALE_STORAGE_KEY));
  } catch {
    return normalizeTextScale(null);
  }
}

function isSetupRoute(): boolean {
  return isSetupHash(window.location.hash);
}

function isSetupHash(hash: string): boolean {
  return parseProjectSetupRoute(hash).kind !== "none";
}

function projectSetupRouteKey(route: ProjectSetupRoute): string {
  if (route.kind === "move") {
    return [
      route.kind,
      route.sourceProjectId,
      route.sourceRequestId ?? "",
      route.targetRequestId ?? "",
    ].join(":");
  }
  if (route.kind === "create")
    return `${route.kind}:${route.requestId ?? ""}:${route.groupId ?? ""}`;
  return route.kind;
}
