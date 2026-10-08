import { MAIN_GRAPH, sameGraphTarget } from "../core/graphTarget";
import type { GraphTargetRef } from "../core/types";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, loadChatReads, markChatRead } from "../core/api";
import {
  chatSummariesForTarget,
  loadChatInventory,
  loadChatTranscript,
  mergeChatSummaryPage,
  reconcileChatSelectionAfterRefresh,
} from "./chatApi";
import {
  chatIdForTask,
  chatReadThrough,
  latestConversation,
  mergeChatReads,
  newlyFinishedChatTaskIds,
  type ChatConversation,
  type ChatKind,
  type DraftConversation,
} from "./chatWorkspace";
import type {
  AgentTask,
  AppView,
  ChatReads,
  ChatSummary,
  ChatTranscript,
  ExperimentOperationalState,
  GraphNode,
} from "../core/types";

/** The run progress that stands in for a cross-graph chat's summary freshness. */
export type ExperimentChatProgress = Pick<
  ExperimentOperationalState,
  "current_operation_id" | "current_status" | "current_last_activity_at"
>;

export interface FloatingChat {
  chatId: string;
  nodeId: string;
}

export interface ChatStateSnapshot {
  floatingChat: FloatingChat | null;
  draftConversations: DraftConversation[];
  selectedChatId: string | null;
  chatReads: ChatReads | null;
  chatSummaries: ChatSummary[];
  inventoryLoaded: boolean;
  chatSummaryTotal: number;
  chatSummaryNextOffset: number;
  chatTranscripts: Map<string, ChatTranscript>;
  selectedCanonicalChat: ChatSummary | null;
  chatTaskStatuses: Map<string, AgentTask["status"]>;
}

interface UseChatStateOptions {
  projectId: string | null;
  apiBase: string;
  graphTarget?: GraphTargetRef;
  selectedExperimentChatId: string | null;
  selectedExperimentChatTarget?: GraphTargetRef;
  selectedExperimentChatFreshness?: string;
  isActiveProject: (projectId: string) => boolean;
  visibleTranscriptIds: (selectedChatId: string | null, floatingChatId: string | null) => string[];
  reportError: (message: string) => void;
}

export function cloneChatStateSnapshot(snapshot: ChatStateSnapshot): ChatStateSnapshot {
  return {
    ...snapshot,
    floatingChat: snapshot.floatingChat ? { ...snapshot.floatingChat } : null,
    draftConversations: [...snapshot.draftConversations],
    chatSummaries: [...snapshot.chatSummaries],
    chatTranscripts: new Map(snapshot.chatTranscripts),
    chatTaskStatuses: new Map(snapshot.chatTaskStatuses),
  };
}

export function visibleChatTranscriptIds(
  view: AppView,
  selectedChatId: string | null,
  floatingChatId: string | null,
  experimentChatId: string | null,
): string[] {
  return [
    ...new Set([
      ...(view === "chats" && selectedChatId ? [selectedChatId] : []),
      ...(view === "execution" && experimentChatId ? [experimentChatId] : []),
      ...(floatingChatId ? [floatingChatId] : []),
    ]),
  ];
}

export function visibleUnreadChatId(
  view: AppView,
  selectedChatId: string | null,
  experimentChatId: string | null,
  agentsBoard = false,
): string | null {
  // The Agents board shows no transcript, so it reads no chat.
  if (view === "chats") return agentsBoard ? null : selectedChatId;
  if (view === "execution") return experimentChatId;
  return null;
}

export function visibleChatTranscriptTarget(
  chatId: string,
  experimentChatId: string | null,
  experimentChatTarget: GraphTargetRef,
  graphTarget: GraphTargetRef,
): GraphTargetRef {
  // The Runs panel routes an Experiment by its own graph, which it keeps
  // separate from the graph being viewed: its href carries `branch`, while the
  // viewed target comes from `branch_id`. A branch-scoped Experiment therefore
  // selects a branch chat while the app still views main, and loading that
  // transcript against main can only 404.
  return chatId === experimentChatId ? experimentChatTarget : graphTarget;
}

export function experimentChatFreshnessToken(
  experimentChatId: string | null,
  operational: ExperimentChatProgress | null,
  experimentChatTarget: GraphTargetRef,
  graphTarget: GraphTargetRef,
): string {
  // A chat on another graph never enters the viewed graph's summaries, so the
  // summary `updated_at` that refires this fetch for every other chat never
  // arrives. The run's own backend-exported progress stands in: it advances
  // when the turn does, which is exactly when a new transcript exists.
  if (!experimentChatId || sameGraphTarget(experimentChatTarget, graphTarget)) return "";
  return [
    operational?.current_operation_id ?? "",
    operational?.current_status ?? "",
    operational?.current_last_activity_at ?? "",
  ].join("|");
}

export function transcriptAbsenceIsExpected(
  chatId: string,
  experimentChatId: string | null,
  error: unknown,
): boolean {
  // The Experiment chat is the one transcript loaded without a summary to prove
  // it exists, because a run's chat need not be on the current summary page.
  // Until a Work turn is captured it has no graph transcript at all, so its 404
  // is the ordinary state of a run that has just started, not a failure.
  return chatId === experimentChatId && error instanceof ApiError && error.status === 404;
}

export function shouldLoadVisibleChatTranscript(
  chatId: string,
  summaries: readonly Pick<ChatSummary, "chat_id" | "message_count">[],
  selectedExperimentChatId: string | null,
): boolean {
  const summary = summaries.find((item) => item.chat_id === chatId);
  if (summary) return summary.message_count > 0;
  return chatId === selectedExperimentChatId;
}

export function useChatState({
  projectId,
  apiBase,
  graphTarget = MAIN_GRAPH,
  selectedExperimentChatId,
  selectedExperimentChatTarget = MAIN_GRAPH,
  selectedExperimentChatFreshness = "",
  isActiveProject,
  visibleTranscriptIds,
  reportError,
}: UseChatStateOptions) {
  const [floatingChat, setFloatingChatState] = useState<FloatingChat | null>(null);
  const [draftConversations, setDraftConversations] = useState<DraftConversation[]>([]);
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
  const [chatReads, setChatReadsState] = useState<ChatReads | null>(null);
  const [chatSummaries, setChatSummaries] = useState<ChatSummary[]>([]);
  const [inventoryLoaded, setInventoryLoaded] = useState(false);
  const [chatSummaryTotal, setChatSummaryTotal] = useState(0);
  const [chatSummaryNextOffset, setChatSummaryNextOffset] = useState(0);
  const [chatSummariesLoading, setChatSummariesLoading] = useState(false);
  const [chatTranscripts, setChatTranscripts] = useState<Map<string, ChatTranscript>>(
    () => new Map(),
  );
  const [selectedCanonicalChat, setSelectedCanonicalChat] = useState<ChatSummary | null>(null);
  const chatTaskStatuses = useRef<Map<string, AgentTask["status"]>>(new Map());
  const chatSummariesRef = useRef<ChatSummary[]>([]);
  const selectedChatIdRef = useRef<string | null>(null);
  const selectedCanonicalChatRef = useRef<ChatSummary | null>(null);
  const chatSummaryRefreshGeneration = useRef(0);
  const chatReadsRef = useRef<ChatReads | null>(null);
  // Summary refreshes and archive changes both fetch chat-reads; only the newest
  // fetch may replace its archive and finish projection.
  const chatReadsFetch = useRef(0);
  const setChatReads = useCallback((next: ChatReads | null) => {
    chatReadsRef.current = next;
    setChatReadsState(next);
  }, []);

  const inventoryChatSummaries = useMemo(
    () =>
      selectedCanonicalChat
        ? mergeChatSummaryPage(chatSummaries, [selectedCanonicalChat], "append")
        : chatSummaries,
    [chatSummaries, selectedCanonicalChat],
  );
  const visibleChatSummaries = useMemo(
    () => chatSummariesForTarget(inventoryChatSummaries, graphTarget),
    [inventoryChatSummaries, graphTarget],
  );
  const visibleChatIds = useMemo(
    () => visibleTranscriptIds(selectedChatId, floatingChat?.chatId ?? null),
    [floatingChat?.chatId, selectedChatId, visibleTranscriptIds],
  );
  const visibleChatVersions = visibleChatIds
    .map((chatId) => {
      const summary = inventoryChatSummaries.find((item) => item.chat_id === chatId);
      return `${chatId}:${summary?.updated_at ?? ""}:${summary?.message_count ?? ""}`;
    })
    .join("|");

  const experimentChatTargetKey =
    selectedExperimentChatTarget.kind === "branch"
      ? `branch:${selectedExperimentChatTarget.branch_id}`
      : "main";

  useEffect(() => {
    if (!apiBase || visibleChatIds.length === 0) return;
    let cancelled = false;
    visibleChatIds.forEach((chatId) => {
      if (
        !shouldLoadVisibleChatTranscript(chatId, inventoryChatSummaries, selectedExperimentChatId)
      ) {
        return;
      }
      void loadChatTranscript(
        apiBase,
        chatId,
        api,
        visibleChatTranscriptTarget(
          chatId,
          selectedExperimentChatId,
          selectedExperimentChatTarget,
          graphTarget,
        ),
      )
        .then((transcript) => {
          if (cancelled) return;
          setChatTranscripts((current) => new Map(current).set(chatId, transcript));
        })
        .catch((error) => {
          if (cancelled) return;
          if (transcriptAbsenceIsExpected(chatId, selectedExperimentChatId, error)) return;
          reportError(
            `Conversation could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
          );
        });
    });
    return () => {
      cancelled = true;
    };
    // The Experiment target is derived per render, so its identity cannot be a
    // dependency; the key below changes exactly when the target does.
    // eslint-disable-next-line react-hooks/exhaustive-deps -- visibleChatVersions keys the visible ids and summaries, experimentChatTargetKey the target
  }, [
    apiBase,
    graphTarget,
    selectedExperimentChatId,
    experimentChatTargetKey,
    selectedExperimentChatFreshness,
    visibleChatVersions,
  ]);

  const selectChat = useCallback((chatId: string | null) => {
    selectedChatIdRef.current = chatId;
    setSelectedChatId(chatId);
    if (selectedCanonicalChatRef.current?.chat_id !== chatId) {
      selectedCanonicalChatRef.current = null;
      setSelectedCanonicalChat(null);
    }
  }, []);

  const setFloatingChat = useCallback((next: FloatingChat | null) => {
    setFloatingChatState(next);
  }, []);

  const selectCanonicalChat = useCallback(
    (transcript: ChatTranscript) => {
      selectChat(transcript.chat_id);
      selectedCanonicalChatRef.current = transcript;
      setSelectedCanonicalChat(transcript);
      setChatTranscripts((current) => new Map(current).set(transcript.chat_id, transcript));
      setFloatingChatState(null);
    },
    [selectChat],
  );

  // A chat that still needs a human can be listed from its open task before its
  // summary page is loaded. Selecting it loads its transcript through the same
  // canonical selection an explicit chat link uses, so it shows its history and
  // title. A 404 means no turn has been captured yet; the task alone is the chat.
  const selectListedConversation = useCallback(
    (chatId: string) => {
      const summary = chatSummariesRef.current.find((item) => item.chat_id === chatId);
      if (summary && !sameGraphTarget(summary.graph_target, graphTarget)) return;
      selectChat(chatId);
      if (!apiBase || summary) return;
      const generation = chatSummaryRefreshGeneration.current;
      void loadChatTranscript(apiBase, chatId, api, graphTarget)
        .then((transcript) => {
          if (
            generation === chatSummaryRefreshGeneration.current &&
            selectedChatIdRef.current === chatId
          )
            selectCanonicalChat(transcript);
        })
        .catch((error) => {
          if (generation !== chatSummaryRefreshGeneration.current) return;
          if (error instanceof ApiError && error.status === 404) return;
          reportError(
            `Conversation could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
          );
        });
    },
    [apiBase, graphTarget, reportError, selectCanonicalChat, selectChat],
  );

  const reconcileFloatingChat = useCallback(
    (nodes: Record<string, GraphNode>, retainMissing: boolean) => {
      setFloatingChatState((current) =>
        current && (nodes[current.nodeId] || retainMissing) ? current : null,
      );
    },
    [],
  );

  const startConversation = useCallback(
    (kind: ChatKind, node: GraphNode | null, projectTitle: string): string => {
      const chatId = window.crypto.randomUUID();
      const draft: DraftConversation = {
        chatId,
        kind,
        nodeId: node?.id ?? null,
        title: node?.title ?? projectTitle,
      };
      setDraftConversations((current) => [draft, ...current]);
      return chatId;
    },
    [],
  );

  /** An unsent draft lives only here, so removing it deletes nothing on the server. */
  const discardDraft = useCallback(
    (chatId: string) => {
      setDraftConversations((current) => current.filter((draft) => draft.chatId !== chatId));
      if (selectedChatIdRef.current === chatId) selectChat(null);
    },
    [selectChat],
  );

  const ensureConversation = useCallback(
    (
      conversations: ChatConversation[],
      kind: ChatKind,
      node: GraphNode | null,
      projectTitle: string,
    ): string => {
      const existing = latestConversation(conversations, kind, node?.id ?? null, graphTarget);
      return existing?.chatId ?? startConversation(kind, node, projectTitle);
    },
    [graphTarget, startConversation],
  );

  const refreshChatSummaries = useCallback(
    async (requestedProjectId: string, base: string) => {
      const generation = ++chatSummaryRefreshGeneration.current;
      setChatSummariesLoading(true);
      try {
        const readsFetch = ++chatReadsFetch.current;
        const [nextSummaries, reads] = await Promise.all([
          loadChatInventory(base, api),
          loadChatReads(`${base}/chat-reads?inventory=true`),
        ]);
        if (
          !isActiveProject(requestedProjectId) ||
          generation !== chatSummaryRefreshGeneration.current
        )
          return;
        const selectedId = selectedChatIdRef.current;
        const previousSummary = selectedId
          ? (chatSummariesRef.current.find((summary) => summary.chat_id === selectedId) ??
            (selectedCanonicalChatRef.current?.chat_id === selectedId
              ? selectedCanonicalChatRef.current
              : null))
          : null;
        let validation: ChatTranscript | null | undefined;
        if (
          selectedId &&
          previousSummary &&
          !nextSummaries.some((summary) => summary.chat_id === selectedId)
        ) {
          try {
            validation = await loadChatTranscript(base, selectedId, api, graphTarget);
          } catch (error) {
            if (error instanceof ApiError && error.status === 404) validation = null;
            else throw error;
          }
        }
        if (
          !isActiveProject(requestedProjectId) ||
          generation !== chatSummaryRefreshGeneration.current
        )
          return;
        if (readsFetch === chatReadsFetch.current) {
          setChatReads(mergeChatReads(chatReadsRef.current, reads));
        }
        chatSummariesRef.current = nextSummaries;
        setChatSummaries(nextSummaries);
        setInventoryLoaded(true);
        setChatSummaryTotal(nextSummaries.length);
        setChatSummaryNextOffset(nextSummaries.length);
        if (selectedChatIdRef.current === selectedId) {
          const reconciliation = reconcileChatSelectionAfterRefresh(
            selectedId,
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
          } else if (reconciliation.deleteTranscript && selectedId) {
            setDraftConversations((current) =>
              current.filter((draft) => draft.chatId !== selectedId),
            );
            setChatTranscripts((current) => {
              if (!current.has(selectedId)) return current;
              const next = new Map(current);
              next.delete(selectedId);
              return next;
            });
          }
        }
      } finally {
        if (
          isActiveProject(requestedProjectId) &&
          generation === chatSummaryRefreshGeneration.current
        ) {
          setChatSummariesLoading(false);
        }
      }
    },
    [graphTarget, isActiveProject, setChatReads],
  );

  const recordTaskUpdates = useCallback((tasks: AgentTask[]) => {
    const previousStatuses = chatTaskStatuses.current;
    const nextStatuses = new Map(previousStatuses);
    const completedChatTasks = newlyFinishedChatTaskIds(tasks, previousStatuses);
    for (const task of tasks) {
      if (!chatIdForTask(task)) continue;
      nextStatuses.set(task.operation_id, task.status);
    }
    chatTaskStatuses.current = nextStatuses;
    return completedChatTasks.length > 0;
  }, []);

  const recordWatcherResults = useCallback((tasks: AgentTask[]) => {
    const unseen = tasks.filter(
      (task) =>
        task.request.trigger === "watcher" &&
        !chatTaskStatuses.current.has(task.operation_id) &&
        task.finished,
    );
    return unseen.length > 0;
  }, []);

  const refreshChatReads = useCallback(async () => {
    if (!projectId || !apiBase) return;
    const requestedProjectId = projectId;
    const readsFetch = ++chatReadsFetch.current;
    const reads = await loadChatReads(`${apiBase}/chat-reads?inventory=true`);
    if (isActiveProject(requestedProjectId) && readsFetch === chatReadsFetch.current) {
      setChatReads(mergeChatReads(chatReadsRef.current, reads));
    }
  }, [apiBase, isActiveProject, projectId, setChatReads]);

  // Reading moves the marker at once; the server's answer then merges into it.
  const markVisibleChatRead = useCallback(
    (tasks: AgentTask[], visibleChatId: string | null) => {
      const current = chatReadsRef.current;
      if (!visibleChatId || !current || !projectId || !apiBase) return;
      const requestedProjectId = projectId;
      const path = `${apiBase}/chats/${encodeURIComponent(visibleChatId)}/read`;
      const readThrough = chatReadThrough(tasks, current, visibleChatId);
      const marker = current.reads[visibleChatId] ?? current.baseline;
      if (!readThrough || Date.parse(readThrough) <= Date.parse(marker)) return;
      setChatReads({ ...current, reads: { ...current.reads, [visibleChatId]: readThrough } });
      void markChatRead(`${path}?inventory=true`, readThrough)
        .then((reads) => {
          const latest = chatReadsRef.current;
          // Only the markers: a fetch may have refreshed the archive projection since.
          if (isActiveProject(requestedProjectId) && latest) {
            setChatReads({ ...latest, reads: mergeChatReads(latest, reads).reads });
          }
        })
        .catch((error) => {
          // Undo the optimistic marker, unless a newer one replaced it, so the
          // next view retries instead of trusting a write the server never saw.
          const latest = chatReadsRef.current;
          if (isActiveProject(requestedProjectId) && latest?.reads[visibleChatId] === readThrough) {
            const reads = { ...latest.reads };
            if (visibleChatId in current.reads) reads[visibleChatId] = current.reads[visibleChatId];
            else delete reads[visibleChatId];
            setChatReads({ ...latest, reads });
          }
          reportError(
            `Chat could not be marked read: ${error instanceof Error ? error.message : String(error)}`,
          );
        });
    },
    [apiBase, isActiveProject, projectId, reportError, setChatReads],
  );

  const resetProjectChats = useCallback(() => {
    setFloatingChatState(null);
    setDraftConversations([]);
    selectChat(null);
    setChatReads(null);
    chatReadsFetch.current += 1;
    chatSummaryRefreshGeneration.current += 1;
    chatSummariesRef.current = [];
    setChatSummaries([]);
    setInventoryLoaded(false);
    setChatSummaryTotal(0);
    setChatSummaryNextOffset(0);
    setChatSummariesLoading(false);
    setChatTranscripts(new Map());
    chatTaskStatuses.current = new Map();
  }, [selectChat, setChatReads]);

  const restoreProjectChats = useCallback(
    (snapshot: ChatStateSnapshot, nodes: Record<string, GraphNode>) => {
      setFloatingChatState(
        snapshot.floatingChat && nodes[snapshot.floatingChat.nodeId]
          ? { ...snapshot.floatingChat }
          : null,
      );
      setDraftConversations([...snapshot.draftConversations]);
      selectChat(snapshot.selectedChatId);
      setChatReads(snapshot.chatReads);
      chatReadsFetch.current += 1;
      chatSummaryRefreshGeneration.current += 1;
      chatSummariesRef.current = [...snapshot.chatSummaries];
      setChatSummaries([...snapshot.chatSummaries]);
      setInventoryLoaded(snapshot.inventoryLoaded);
      setChatSummaryTotal(snapshot.chatSummaryTotal);
      setChatSummaryNextOffset(snapshot.chatSummaryNextOffset);
      setChatSummariesLoading(false);
      setChatTranscripts(new Map(snapshot.chatTranscripts));
      selectedCanonicalChatRef.current = snapshot.selectedCanonicalChat;
      setSelectedCanonicalChat(snapshot.selectedCanonicalChat);
      chatTaskStatuses.current = new Map(snapshot.chatTaskStatuses);
    },
    [selectChat, setChatReads],
  );

  const snapshot: ChatStateSnapshot = {
    floatingChat,
    draftConversations,
    selectedChatId,
    chatReads,
    chatSummaries,
    inventoryLoaded,
    chatSummaryTotal,
    chatSummaryNextOffset,
    chatTranscripts,
    selectedCanonicalChat,
    chatTaskStatuses: chatTaskStatuses.current,
  };

  return {
    snapshot,
    chatSummariesLoading,
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
    recordTaskUpdates,
    recordWatcherResults,
    markVisibleChatRead,
    refreshChatReads,
    resetProjectChats,
    restoreProjectChats,
  };
}
