import {
  AlertTriangle,
  ArrowLeft,
  BookOpenText,
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
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  isActiveTask,
  parseDismissedTaskIds,
  projectActivityTask,
  serializeDismissedTaskIds,
  taskKindLabel,
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
import { AgentTaskActivity } from "./components/AgentTaskActivity";
import { AgentTaskInspector } from "./components/AgentTaskInspector";
import { AttentionRail } from "./components/AttentionRail";
import { DetailDrawer } from "./components/DetailDrawer";
import { DraggableWindow } from "./components/DraggableWindow";
import { RunDialog } from "./components/RunDialog";
import {
  applyHumanDraft,
  deserializeHumanDraft,
  emptyHumanDraft,
  humanDraftChangeCount,
  humanDraftStorageKey,
  normalizeHumanDraft,
  serializeHumanDraft,
  stageAmbiguityDecision,
  stageNodeEdit,
  stageNodeEditStart,
  stageAttemptRelease,
  stageNodeStanding,
  stageProposalDecision,
  stageCustomNode,
  stageOntology,
  unstageCustomNode,
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
  GraphNode,
  GraphState,
  Health,
  PaperSnapshot,
  ProjectCard,
  ProjectSnapshot,
  TrustView,
  ValidationMessage,
  WatcherRecord,
} from "./types";
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

const AttentionOverview = lazy(() =>
  import("./views/GraphViews").then((module) => ({ default: module.AttentionOverview })),
);
const DagView = lazy(() =>
  import("./views/GraphViews").then((module) => ({ default: module.DagView })),
);
const ExecutionView = lazy(() =>
  import("./views/GraphViews").then((module) => ({ default: module.ExecutionView })),
);
const GlossaryView = lazy(() =>
  import("./views/GraphViews").then((module) => ({ default: module.GlossaryView })),
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
  { view: "glossary", label: "Glossary", icon: <BookOpenText size={14} /> },
  { view: "paper", label: "Paper", icon: <FileText size={14} /> },
  { view: "settings", label: "Settings", icon: <Settings2 size={14} /> },
  { view: "chats", label: "Chats", icon: <MessageCircle size={14} /> },
];

const PROJECT_HEADER_COLLAPSED_KEY = "rcp:project-header-collapsed";

type ProjectReconciliation = "opening" | "reconciling" | "authoritative" | "failed";

export default function App() {
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const [identityReady, setIdentityReady] = useState(false);
  const [identityIssue, setIdentityIssue] = useState<string | null>(null);
  const [verifiedHealth, setVerifiedHealth] = useState<Health | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [desktopUpdate, setDesktopUpdate] = useState<DesktopUpdate | null>(null);
  const [updateExpanded, setUpdateExpanded] = useState(false);
  const [updateApplying, setUpdateApplying] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [pendingDesktopProjectId, setPendingDesktopProjectId] = useState<string | null>(null);
  const [desktopAccessError, setDesktopAccessError] = useState<string | null>(null);
  const [projectId, setProjectId] = useState<string | null>(() => projectIdFromHash());
  const [setupOpen, setSetupOpen] = useState(() => isSetupRoute());
  const [projects, setProjects] = useState<ProjectCard[]>([]);
  const [project, setProject] = useState<ProjectSnapshot | null>(null);
  const [projectHeaderCollapsed, setProjectHeaderCollapsed] = useState(() =>
    readProjectHeaderCollapsed(projectId),
  );
  const [graph, setGraph] = useState<GraphState>(emptyGraph);
  const [paper, setPaper] = useState<PaperSnapshot | null>(null);
  const [view, setView] = useState<AppView>("overview");
  const [trustView, setTrustView] = useState<TrustView>(
    () => (localStorage.getItem("rcp:trust-view") as TrustView) || "working",
  );
  const [runScope, setRunScope] = useState<string[]>([]);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
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
  const [textScale, setTextScale] = useState(() =>
    normalizeTextScale(localStorage.getItem(TEXT_SCALE_STORAGE_KEY)),
  );
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
  const verifiedHealthRef = useRef<Health | null>(null);
  const initialShowHandshake = useRef(false);
  const chatTaskStatuses = useRef<Map<string, AgentTask["status"]>>(new Map());
  const chatSummariesRef = useRef<ChatSummary[]>([]);
  const selectedChatIdRef = useRef<string | null>(null);
  const selectedCanonicalChatRef = useRef<ChatSummary | null>(null);
  const chatSummaryRefreshGeneration = useRef(0);
  activeProjectId.current = projectId;
  const [notice, setNotice] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const apiBase = projectId ? `/api/projects/${encodeURIComponent(projectId)}` : "";

  useEffect(() => {
    if (!notice) return;
    const timeoutId = window.setTimeout(() => {
      setNotice((current) => (current === notice ? null : current));
    }, NOTICE_TIMEOUT_MS);
    return () => window.clearTimeout(timeoutId);
  }, [notice]);

  const selectChat = useCallback((chatId: string | null) => {
    selectedChatIdRef.current = chatId;
    setSelectedChatId(chatId);
    if (selectedCanonicalChatRef.current?.chat_id !== chatId) {
      selectedCanonicalChatRef.current = null;
      setSelectedCanonicalChat(null);
    }
  }, []);

  const applyProjectSnapshot = useCallback(
    (nextProject: ProjectSnapshot, preserveReadiness: boolean) => {
      const nextGraph = nextProject.graph;
      setProject((current) =>
        preserveReadiness ? preserveProjectReadiness(nextProject, current) : nextProject,
      );
      setGraph(nextGraph);
      setPaper(nextProject.paper);
      setSelectedNode((current) => (current ? (nextGraph.nodes[current.id] ?? current) : null));
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
        void loadProjectReadiness(base)
          .then((readiness) => {
            if (activeProjectId.current !== requestedProjectId) return;
            setProject((current) =>
              current?.id === nextProject.id ? { ...current, ...readiness } : current,
            );
          })
          .catch((error) => {
            if (activeProjectId.current !== requestedProjectId) return;
            setNotice({
              kind: "error",
              text: error instanceof Error ? error.message : String(error),
            });
          });
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
      setSetupOpen(isSetupRoute());
      setProjectId(projectIdFromHash());
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  useEffect(() => {
    if (!identityReady || identityIssue) return;
    setLoading(true);
    setProjectReconciliation("opening");
    authoritativeProjectId.current = null;
    setNotice(null);
    setProject(null);
    setGraph(emptyGraph);
    setPaper(null);
    setSelectedNode(null);
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
    setWatchers([]);
    setTaskInspectorId(null);
    setInspectedTask(null);
    setActivityTaskId(null);
    setDismissedTaskIds(readDismissedTaskIds(projectId));
    setDismissedHistoryNoticeIds(readDismissedHistoryNoticeIds(projectId));
    setProjectHeaderCollapsed(readProjectHeaderCollapsed(projectId));
    setHumanDraft(null);
    setSyncingDraft(false);
    setView("overview");
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
    try {
      setHumanDraft(deserializeHumanDraft(localStorage.getItem(humanDraftStorageKey(projectId))));
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
    }
    let cancelled = false;
    const openProject = async () => {
      const cachedPath = `/api/projects/${encodeURIComponent(projectId)}/cached`;
      try {
        const cachedProject = await api<ProjectSnapshot>(cachedPath);
        if (cancelled || activeProjectId.current !== projectId) return;
        applyProjectSnapshot(cachedProject, false);
        setProjectReconciliation("reconciling");
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
        if (authoritativeProjectId.current !== projectId) setProjectReconciliation("failed");
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
    selectChat,
    setupOpen,
  ]);

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

  useEffect(() => localStorage.setItem("rcp:trust-view", trustView), [trustView]);

  useEffect(() => {
    localStorage.setItem(TEXT_SCALE_STORAGE_KEY, String(textScale));
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
  const selectedExperimentControl = useMemo<ExperimentControlState | null>(() => {
    if (!project || selectedNode?.type !== "experiment") return null;
    const control = project.experiment_control?.[selectedNode.id];
    if (!control) return null;
    const operationActive = tasks.some(
      (task) =>
        isActiveTask(task) &&
        task.request.patch_kind === "experiment_loop" &&
        task.request.control_node_id === selectedNode.id,
    );
    return operationActive && !control.active ? { ...control, active: true } : control;
  }, [project, selectedNode, tasks]);
  const retryConfig = useMemo(
    () => (retryTask && project ? taskRetryConfig(retryTask, project) : null),
    [project, retryTask],
  );
  const mutationsDisabled = graphMutationsDisabled(graph);
  const presentedGraph = useMemo(
    () => (mutationsDisabled ? graph : applyHumanDraft(graph, humanDraft)),
    [graph, humanDraft, mutationsDisabled],
  );
  const openNode = (node: GraphNode | null) => {
    if (!node) return;
    setDockedNodeIds((current) => current.filter((nodeId) => nodeId !== node.id));
    setSelectedNode(node);
  };
  const openNodeById = (nodeId: string) => openNode(presentedGraph.nodes[nodeId] ?? null);
  const dockNode = (nodeId: string) => {
    setDockedNodeIds((current) => (current.includes(nodeId) ? current : [...current, nodeId]));
    setSelectedNode((current) => (current?.id === nodeId ? null : current));
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
  const draftIsStale = Boolean(humanDraft && humanDraft.base_revision !== graph.revision);
  const latestTask = tasks[0] ?? null;
  const activityTask = projectActivityTask(tasks, activityTaskId);
  const visibleTask =
    activityTask && !dismissedTaskIds.has(activityTask.operation_id) ? activityTask : null;
  const chatsIndicator = chatIndicator(tasks, unreadChatTaskIds);

  const changeAppTextScale = (action: TextScaleAction) => {
    setTextScale((current) => changeTextScale(current, action));
  };

  const ensureConversation = (kind: ChatKind, node: GraphNode | null = null): string => {
    const existing = latestConversation(conversations, kind, node?.id ?? null);
    if (existing) return existing.chatId;
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

  const openChats = (preferredChatId?: string | null) => {
    const nextChatId =
      preferredChatId ??
      chatEntryConversationId(conversations, activityTask, unreadChatTaskIds, selectedChatId);
    selectChat(nextChatId);
    setFloatingChat(null);
    setView("chats");
  };

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
        if (current.status === "succeeded" && current.applied_revision) {
          try {
            await reload();
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
  }, [activeTask, projectId, reload]);

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
        const [nextWatchers, nextTasks] = await Promise.all([
          api<WatcherRecord[]>(`${base}/watchers`),
          api<AgentTask[]>(`${base}/tasks`),
        ]);
        if (!stopped && activeProjectId.current === requestedProjectId) {
          const unseenWatcherResults = nextTasks.filter(
            (task) =>
              task.request.trigger === "watcher" &&
              !chatTaskStatuses.current.has(task.operation_id) &&
              (task.status === "succeeded" ||
                task.status === "failed" ||
                task.status === "interrupted"),
          );
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
  }, [projectId, refreshChatSummaries, watchersAwaitingDelivery]);

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
  const ambiguities = useMemo(
    () => Object.values(graph.ambiguities).filter((item) => item.status === "open"),
    [graph],
  );
  const scientificBlockers = useMemo(
    () =>
      Object.values(presentedGraph.nodes).filter(
        (node) =>
          node.type === "blocker" &&
          node.status === "open" &&
          ["scientific", "design"].includes(String(node.blocker_type)),
      ),
    [presentedGraph],
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
    if (!humanDraft || syncingDraft || draftIsStale || mutationsDisabled) return;
    const normalized = normalizeHumanDraft(humanDraft, graph);
    if (humanDraftChangeCount(normalized) === 0) {
      resetHumanDraft();
      return;
    }
    setSyncingDraft(true);
    setNotice(null);
    try {
      const nextGraph = await api<GraphState>(`${apiBase}/sync`, {
        method: "POST",
        body: JSON.stringify(toHumanSyncRequest(normalized)),
      });
      resetHumanDraft();
      setGraph(nextGraph);
      setProject((current) => (current ? projectWithGraph(current, nextGraph) : current));
      setSelectedNode((current) => (current ? (nextGraph.nodes[current.id] ?? current) : null));
      await reload();
      setNotice({ kind: "info", text: `Synced revision ${nextGraph.revision}.` });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setNotice({ kind: "error", text: "Draft base is stale. Reset the draft before syncing." });
        try {
          await reload();
        } catch {}
      } else {
        setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) });
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
    if (taskStartLock.current || taskStarting || activeTask)
      throw new Error("Another agent task is already active for this project.");
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

  // Releasing an attempt is two commitments: the watchers stop now because they
  // are operational, and the attempt closes at Sync because it is graph history.
  const releaseAttempt = async (node: GraphNode, attemptId: string) => {
    if (!apiBase || mutationsDisabled) return;
    updateHumanDraft((draft) => stageAttemptRelease(draft, graph, node.id, attemptId));
    try {
      await api<WatcherRecord[]>(
        `${apiBase}/experiments/${encodeURIComponent(node.id)}/watchers/stop`,
        { method: "POST" },
      );
      setWatchers(await api<WatcherRecord[]>(`${apiBase}/watchers`));
    } catch (error) {
      setNotice({ kind: "error", text: (error as Error).message });
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
    if (taskStartLock.current || taskStarting || activeTask) {
      setNotice({ kind: "error", text: "Another agent task is already active for this project." });
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
      selectChat(chatId);
      setFloatingChat({ chatId, nodeId: node.id });
    } catch (caught) {
      setNotice({ kind: "error", text: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      taskStartLock.current = false;
      setTaskStarting(false);
    }
  };

  const runAgent = async (config: AgentRunConfig, scope: string[], message: string | null) => {
    if (!project || taskStarting || activeTask || mutationsDisabled) return;
    const runKind = graph.revision === 0 ? "seed" : "refresh";
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

  const operateTask = async (task: AgentTask, action: "pause" | "resume" | "retry") => {
    if (taskActionId || (activeTask && activeTask.operation_id !== task.operation_id)) return;
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
      setActivityTaskId(next.operation_id);
      setTaskInspectorId(next.operation_id);
      setInspectedTask(next);
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

  const repairGraphUpdate = async (operationId: string): Promise<void> => {
    if (taskStartLock.current || taskStarting || taskActionId || activeTask) {
      throw new Error("Another agent task is already active for this project.");
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
    if (taskActionId || activeTask || mutationsDisabled) return;
    setTaskActionId(task.operation_id);
    try {
      const next = await api<AgentTask>(`${apiBase}/tasks/${task.operation_id}/retry`, {
        method: "POST",
        body: JSON.stringify(config),
      });
      setTasks((current) => [
        next,
        ...current.filter((item) => item.operation_id !== next.operation_id),
      ]);
      setActivityTaskId(next.operation_id);
      setTaskInspectorId(next.operation_id);
      setInspectedTask(next);
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

  const commitProjectOpen = (id: string) => {
    setSetupOpen(false);
    window.location.hash = `/projects/${id}`;
  };
  const openProject = (id: string) => {
    if (desktop) {
      let storedAcknowledgement: string | null = null;
      try {
        storedAcknowledgement = localStorage.getItem(DESKTOP_FOLDER_ACCESS_ACK_KEY);
      } catch {}
      if (needsDesktopFolderAccessAcknowledgement(true, storedAcknowledgement)) {
        setDesktopAccessError(null);
        setPendingDesktopProjectId(id);
        return;
      }
    }
    commitProjectOpen(id);
  };
  const continueDesktopProjectOpen = () => {
    if (!pendingDesktopProjectId) return;
    try {
      localStorage.setItem(
        DESKTOP_FOLDER_ACCESS_ACK_KEY,
        desktopFolderAccessAcknowledgementValue(),
      );
      const projectToOpen = pendingDesktopProjectId;
      setPendingDesktopProjectId(null);
      setDesktopAccessError(null);
      commitProjectOpen(projectToOpen);
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
    setSetupOpen(false);
    setProjectId(null);
    window.location.hash = "";
  };

  const deleteProject = async (id: string) => {
    await api(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
    setProjects((current) => current.filter((item) => item.id !== id));
    localStorage.removeItem(humanDraftStorageKey(id));
  };

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
  const desktopAccessSurface = pendingDesktopProjectId ? (
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
              setPendingDesktopProjectId(null);
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

  if (!identityReady)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>Verifying the RCP backend</span>
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
      </div>
    );
  if (loading)
    return (
      <div className="app-loading">
        <LoaderCircle className="spin" />
        <span>{projectId ? "Opening project" : "Reading the project index"}</span>
        {updateSurface}
        {desktopAccessSurface}
      </div>
    );
  if (setupOpen)
    return (
      <>
        <ProjectSetup onCancel={returnToProjects} onCreated={openProject} />
        {updateSurface}
        {desktopAccessSurface}
      </>
    );
  if (!projectId)
    return (
      <>
        <ProjectLanding
          projects={projects}
          onOpen={openProject}
          onCreate={openSetup}
          onDelete={deleteProject}
        />
        {updateSurface}
        {desktopAccessSurface}
      </>
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
      </div>
    );

  const attentionCount = pendingProposals.length + ambiguities.length + scientificBlockers.length;
  const showTrustFilter = view === "scientific" || view === "dag" || view === "execution";
  const runKind = graph.revision === 0 ? "seed" : "refresh";
  const replayWarning = replayFailureLabel(graph);

  return (
    <div className="app-shell overview-shell">
      {!projectHeaderCollapsed && (
        <header className={`project-header${draftChangeCount > 0 ? " has-draft" : ""}`}>
          <div className="brand-lockup">
            <button className="project-back" onClick={returnToProjects} aria-label="All projects">
              <ArrowLeft size={16} />
            </button>
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
                  className={`button draft-sync${draftChangeCount > 0 ? " active" : ""}${draftIsStale ? " stale" : ""}`}
                  disabled={
                    mutationsDisabled ||
                    projectReconciliation !== "authoritative" ||
                    draftChangeCount === 0 ||
                    syncingDraft ||
                    draftIsStale ||
                    !project.canonical_state.reachable
                  }
                  title={draftIsStale ? "Draft base is stale" : undefined}
                  aria-label={
                    syncingDraft
                      ? "Syncing staged changes"
                      : draftIsStale
                        ? `Sync conflict, ${draftChangeCount} staged changes`
                        : undefined
                  }
                  onClick={() => void syncHumanDraft()}
                >
                  {syncingDraft ? (
                    <LoaderCircle className="spin" size={14} />
                  ) : draftIsStale ? (
                    <AlertTriangle size={14} />
                  ) : (
                    <CloudUpload size={14} />
                  )}
                  <span>Sync</span>
                  {draftChangeCount > 0 && <small>{draftChangeCount}</small>}
                </button>
              </div>
              <button
                className="button secondary"
                disabled={projectReconciliation !== "authoritative"}
                onClick={() => {
                  const chatId = ensureConversation("project_chat");
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
                disabled={!latestTask}
                aria-label={activeTask ? "Agent tasks, task in progress" : "Agent tasks"}
                onClick={() => latestTask && setTaskInspectorId(latestTask.operation_id)}
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
                  Boolean(activeTask) ||
                  taskStarting
                }
                aria-label={
                  activeTask
                    ? `${taskKindLabel(activeTask.kind)} in progress`
                    : runKind === "seed"
                      ? "Seed project"
                      : "Refresh project"
                }
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
          <button
            className="project-tabs-back project-back"
            onClick={returnToProjects}
            aria-label="All projects"
          >
            <ArrowLeft size={16} />
          </button>
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
            onClick={() => (item.view === "chats" ? openChats() : setView(item.view))}
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
        {visibleTask && (!chatIdForTask(visibleTask) || view === "chats") && (
          <AgentTaskActivity
            task={visibleTask}
            actionBusy={Boolean(
              taskActionId || (activeTask && activeTask.operation_id !== visibleTask.operation_id),
            )}
            mutatingActionsDisabled={mutationsDisabled && taskMayMutateGraph(visibleTask)}
            onPause={() => void operateTask(visibleTask, "pause")}
            onResume={() => void operateTask(visibleTask, "resume")}
            onRetry={() => requestRetry(visibleTask)}
            onInspect={() => setTaskInspectorId(visibleTask.operation_id)}
            onDismiss={() => dismissTaskNotification(visibleTask.operation_id)}
          />
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
        {(project.coverage.sessions_skipped.length > 0 ||
          project.coverage.repositories_never_seen.length > 0) &&
          view === "overview" && (
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
                onClick={() => setView("scientific")}
              >
                <GitBranch size={13} /> Research
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={view === "dag"}
                className={view === "dag" ? "active" : ""}
                onClick={() => setView("dag")}
              >
                <Network size={13} /> DAG
              </button>
            </div>
          )}
          {view === "overview" && (
            <ProjectOverview
              project={projectWithGraph(project, presentedGraph)}
              graph={presentedGraph}
              onNavigate={setView}
            />
          )}
          {view === "attention" && (
            <div className="attention-page">
              <AttentionOverview graph={presentedGraph} onSelectNode={openNode} />
              <AttentionRail
                proposals={pendingProposals}
                ambiguities={ambiguities}
                blockers={scientificBlockers}
                draft={mutationsDisabled ? null : humanDraft}
                mutationsDisabled={mutationsDisabled}
                onDecision={(proposal, decision) =>
                  updateHumanDraft((draft) => stageProposalDecision(draft, proposal.id, decision))
                }
                onAmbiguity={(ambiguity, status) =>
                  updateHumanDraft((draft) => stageAmbiguityDecision(draft, ambiguity.id, status))
                }
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
              relationFocusNodeId={dagRelationFocusId}
              onClearRelationFocus={() => setDagRelationFocusId(null)}
              onSelectNode={openNode}
            />
          )}
          {view === "execution" && (
            <ExecutionView
              graph={presentedGraph}
              trustView={trustView}
              tasks={tasks}
              dismissedTaskIds={dismissedTaskIds}
              lastRefreshAt={project.last_refresh_at}
              onInspectTask={setTaskInspectorId}
              onDismissTask={dismissTaskNotification}
              onSelectNode={openNode}
            />
          )}
          {view === "glossary" && <GlossaryView graph={presentedGraph} />}
          {view === "paper" && (
            <PaperWorkspace
              apiBase={apiBase}
              project={project}
              initialPaper={paper}
              tasks={tasks}
              activeTask={activeTask}
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
              ontology={presentedGraph.ontology}
              canonicalOntology={graph.ontology}
              ontologyStaged={Boolean(humanDraft?.ontology)}
              showDisplaySettings={desktop}
              textScale={textScale}
              onTextScaleChange={changeAppTextScale}
              onOntologyChange={(ontology) =>
                updateHumanDraft((draft) => stageOntology(draft, graph, ontology))
              }
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
              runScope={runScope}
              tasks={tasks}
              activeTask={activeTask}
              watchers={watchers}
              graphChangesDisabled={mutationsDisabled}
              unreadTaskIds={unreadChatTaskIds}
              chatTranscripts={chatTranscripts}
              hasMore={chatSummaryNextOffset < chatSummaryTotal}
              loadingMore={chatSummariesLoading}
              onSelect={selectChat}
              onLoadMore={() => void loadMoreChatSummaries()}
              onStartTask={startAgentTask}
              onInspectTask={setTaskInspectorId}
              onOpenInbox={() => setView("attention")}
              onRepairGraphUpdate={repairGraphUpdate}
              onStopWatcher={(watcherId) => void stopWatcher(watcherId)}
            />
          )}
        </Suspense>
      </main>

      {selectedNode && (
        <DetailDrawer
          node={presentedGraph.nodes[selectedNode.id] ?? selectedNode}
          edges={Object.values(presentedGraph.edges)}
          allNodes={presentedGraph.nodes}
          glossary={presentedGraph.glossary}
          beliefTransitions={graph.belief_transitions}
          validationMessages={graph.validation_messages}
          ontology={presentedGraph.ontology}
          mutationsDisabled={mutationsDisabled}
          stagedNewNode={Boolean(humanDraft?.custom_nodes[selectedNode.id])}
          experimentControl={selectedExperimentControl}
          experimentRunDisabled={Boolean(activeTask)}
          experimentRunBusy={taskStarting}
          onUnstage={() => {
            updateHumanDraft((draft) => unstageCustomNode(draft, selectedNode.id));
            setSelectedNode(null);
          }}
          onClose={() => setSelectedNode(null)}
          onDock={() => dockNode(selectedNode.id)}
          onBeginEdit={() =>
            updateHumanDraft((draft) => stageNodeEditStart(draft, graph, selectedNode.id))
          }
          onStanding={(standing) =>
            updateHumanDraft((draft) => stageNodeStanding(draft, graph, selectedNode.id, standing))
          }
          onStage={(changes) =>
            updateHumanDraft((draft) => stageNodeEdit(draft, graph, selectedNode.id, changes))
          }
          onRunExperiment={() =>
            void runExperiment(presentedGraph.nodes[selectedNode.id] ?? selectedNode)
          }
          onReleaseAttempt={(attemptId) =>
            void releaseAttempt(presentedGraph.nodes[selectedNode.id] ?? selectedNode, attemptId)
          }
          onOpenChat={() => {
            const node = presentedGraph.nodes[selectedNode.id] ?? selectedNode;
            const chatId = ensureConversation("node_chat", node);
            selectChat(chatId);
            setFloatingChat({ chatId, nodeId: node.id });
          }}
          onExploreRelations={() => {
            setDagRelationFocusId(selectedNode.id);
            setView("dag");
          }}
          onSelectNode={openNodeById}
        />
      )}
      {floatingChat && (
        <DraggableWindow className="node-chat-window" kind="chat">
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
              conversationTitle={
                conversations.find((conversation) => conversation.chatId === floatingChat.chatId)
                  ?.title
              }
              runScope={runScope}
              tasks={tasks}
              activeTask={activeTask}
              watchers={watchers}
              historyMessages={chatTranscripts.get(floatingChat.chatId)?.messages}
              chatId={floatingChat.chatId}
              presentation="floating"
              graphChangesDisabled={mutationsDisabled}
              onStartTask={startAgentTask}
              onInspectTask={setTaskInspectorId}
              onOpenInbox={() => {
                setFloatingChat(null);
                setView("attention");
              }}
              onRepairGraphUpdate={repairGraphUpdate}
              onStopWatcher={(watcherId) => void stopWatcher(watcherId)}
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
          kind={retryTask.kind === "seed" ? "seed" : "refresh"}
          project={project}
          initialScope={retryTask.request.run_truth_scope || project.default_run_truth_scope}
          initialConfig={retryConfig}
          busy={taskActionId === retryTask.operation_id}
          onClose={() => setRetryTask(null)}
          onRun={(config) => void retryAgentTask(retryTask, config)}
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
  };
}

function taskRetryConfig(task: AgentTask, project: ProjectSnapshot): AgentRunConfig {
  const profile = project.agent_profiles[task.kind === "seed" ? "seed" : "refresh"];
  return {
    provider: task.request.provider || profile.provider,
    model: task.request.model ?? profile.model,
    reasoning: task.request.reasoning || profile.reasoning,
    run_on: task.request.run_on || profile.run_on,
  };
}

function projectIdFromHash(): string | null {
  const match = window.location.hash.match(/^#\/projects\/([^/]+)$/);
  if (!match || match[1] === "new") return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
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
