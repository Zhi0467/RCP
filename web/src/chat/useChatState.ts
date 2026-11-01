import { useProjectTabResources } from "../projects/projectTabResources";
import { useRefreshRuntime, useResource, useResourcePresentation } from "../core/useResource";
import { resourceKeyId, type ResourceView } from "../core/resourceStore";
import {
  chatResourceProfile,
  chatsResourceKey,
  chatsResourceProfile,
  type ChatsResource,
} from "./chatResources";
import { MAIN_GRAPH, sameGraphTarget } from "../core/graphTarget";
import type { GraphTargetRef } from "../core/types";
import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import { ApiError, markChatRead } from "../core/api";
import { chatSummariesForTarget, mergeChatSummaryPage } from "./chatApi";
import {
  chatReadThrough,
  latestConversation,
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
}

interface UseChatStateOptions {
  projectId: string | null;
  apiBase: string;
  graphTarget?: GraphTargetRef;
  selectedExperimentChatId: string | null;
  selectedExperimentChatTarget?: GraphTargetRef;
  isActiveProject: (projectId: string) => boolean;
  visibleTranscriptIds: (selectedChatId: string | null, floatingChatId: string | null) => string[];
  reportError: (message: string) => void;
  /** App's tab/target/entity entry, including visits to another panel. */
  presentationKey?: string;
}

export type ChatUiState = Pick<
  ChatStateSnapshot,
  "floatingChat" | "draftConversations" | "selectedChatId"
>;

export function cloneChatStateSnapshot(snapshot: ChatUiState): ChatUiState {
  return {
    floatingChat: snapshot.floatingChat ? { ...snapshot.floatingChat } : null,
    draftConversations: [...snapshot.draftConversations],
    selectedChatId: snapshot.selectedChatId,
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
  summaries: readonly ChatSummary[] = [],
): GraphTargetRef {
  // A chat listed from another graph, such as a branch orchestrator on main,
  // reads its transcript from its own graph.
  const summary = summaries.find((item) => item.chat_id === chatId);
  if (summary?.conversation_kind === "auto_research") return summary.graph_target;
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

const EMPTY_SUMMARIES: ChatSummary[] = [];

export function useChatState({
  projectId,
  apiBase,
  graphTarget = MAIN_GRAPH,
  selectedExperimentChatId,
  selectedExperimentChatTarget = MAIN_GRAPH,
  visibleTranscriptIds,
  reportError,
  presentationKey = "",
}: UseChatStateOptions) {
  const { store } = useRefreshRuntime();
  const [floatingChat, setFloatingChat] = useState<FloatingChat | null>(null);
  const [draftConversations, setDraftConversations] = useState<DraftConversation[]>([]);
  const [selectedChatId, setSelectedChatId] = useState<string | null>(null);
  const [selectedTarget, setSelectedTarget] = useState<GraphTargetRef | null>(null);
  const inventory = useResource(
    `${projectId}:chat-view`,
    projectId ? chatsResourceProfile(projectId, apiBase) : null,
    presentationKey,
  );
  const chatSummaries = inventory.value?.summaries ?? EMPTY_SUMMARIES;
  const chatReads = inventory.value?.reads ?? null;
  const ids = visibleTranscriptIds(selectedChatId, floatingChat?.chatId ?? null);
  const admittedIds = projectId
    ? ids.filter(
        (chatId) =>
          shouldLoadVisibleChatTranscript(chatId, chatSummaries, selectedExperimentChatId) ||
          (chatId === selectedChatId &&
            selectedTarget !== null &&
            !chatSummaries.some((summary) => summary.chat_id === chatId) &&
            !draftConversations.some((draft) => draft.chatId === chatId)),
      )
    : [];
  const profiles = admittedIds.map((chatId) =>
    chatResourceProfile(
      projectId ?? "",
      chatId === selectedChatId && selectedTarget
        ? selectedTarget
        : visibleChatTranscriptTarget(
            chatId,
            selectedExperimentChatId,
            selectedExperimentChatTarget,
            graphTarget,
            chatSummaries,
          ),
      chatId,
      apiBase,
    ),
  );
  const profileIds = profiles.map((profile) => resourceKeyId(profile.key));
  const profileToken = JSON.stringify(profileIds);
  const entityToken = JSON.stringify(profiles.map((profile) => profile.key.entity));
  const retainedTranscripts = useProjectTabResources(store);
  const transcripts = useResourcePresentation("chat-transcripts", presentationKey, profileIds);
  useLayoutEffect(() => {
    if (projectId) retainedTranscripts.select(projectId, profiles);
    // Resource identity includes the owning graph, independent of the viewed graph.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [retainedTranscripts, projectId, profileToken]);
  const transcriptViews = transcripts.views as readonly ResourceView<ChatTranscript>[];
  const chatTranscripts = useMemo(
    () =>
      new Map(
        transcriptViews.flatMap((view) =>
          view.value ? [[view.value.chat_id, view.value] as const] : [],
        ),
      ),
    [transcriptViews],
  );
  useEffect(() => {
    if (transcripts.error) {
      // Admission failures have no payload view to inspect.
      const index = (JSON.parse(profileToken) as string[]).findIndex(
        (id) => store.view(id).error === transcripts.error,
      );
      const failedChat = (JSON.parse(entityToken) as string[])[index];
      if (
        failedChat &&
        !chatSummaries.some(
          (summary) => summary.chat_id === failedChat && summary.message_count > 0,
        ) &&
        transcriptAbsenceIsExpected(failedChat, selectedExperimentChatId, transcripts.error)
      )
        return;
      reportError(`Conversation could not be loaded: ${String(transcripts.error)}`);
      return;
    }
    const entities = JSON.parse(entityToken) as string[];
    transcriptViews.forEach((view, index) => {
      if (!view.error) return;
      const chatId = entities[index];
      const captured = chatSummaries.some(
        (summary) => summary.chat_id === chatId && summary.message_count > 0,
      );
      if (!captured && transcriptAbsenceIsExpected(chatId, selectedExperimentChatId, view.error))
        return;
      reportError(
        `Conversation could not be loaded: ${view.error instanceof Error ? view.error.message : String(view.error)}`,
      );
    });
  }, [
    store,
    profileToken,
    transcripts.error,
    transcriptViews,
    entityToken,
    selectedExperimentChatId,
    chatSummaries,
    reportError,
  ]);
  useEffect(() => {
    if (inventory.error)
      reportError(
        `Conversations could not be loaded: ${inventory.error instanceof Error ? inventory.error.message : String(inventory.error)}`,
      );
  }, [inventory.error, reportError]);
  const selectedCanonicalChat = selectedChatId
    ? (chatTranscripts.get(selectedChatId) ?? null)
    : null;
  const inventoryChatSummaries = selectedCanonicalChat
    ? mergeChatSummaryPage(chatSummaries, [selectedCanonicalChat], "append")
    : chatSummaries;
  const visibleChatSummaries = chatSummariesForTarget(inventoryChatSummaries, graphTarget);
  const selectChat = useCallback((id: string | null) => {
    setSelectedChatId(id);
    setSelectedTarget(null);
  }, []);
  const selectCanonicalChat = useCallback(
    (transcript: ChatTranscript) => {
      if (!projectId) return;
      const profile = chatResourceProfile(
        projectId,
        transcript.graph_target,
        transcript.chat_id,
        apiBase,
      );
      const id = store.register(projectId, profile);
      store.write(id, transcript);
      setSelectedChatId(transcript.chat_id);
      setSelectedTarget(transcript.graph_target);
      setFloatingChat(null);
    },
    [projectId, apiBase, store],
  );
  const selectListedConversation = useCallback(
    (id: string, target?: GraphTargetRef) => {
      const summary = chatSummaries.find((item) => item.chat_id === id);
      if (
        summary &&
        summary.conversation_kind !== "auto_research" &&
        !sameGraphTarget(summary.graph_target, graphTarget)
      )
        return;
      setSelectedChatId(id);
      setSelectedTarget(target ?? summary?.graph_target ?? graphTarget);
    },
    [chatSummaries, graphTarget],
  );
  const reconcileFloatingChat = useCallback(
    (nodes: Record<string, GraphNode>, retainMissing: boolean) => {
      setFloatingChat((current) =>
        current && (nodes[current.nodeId] || retainMissing) ? current : null,
      );
    },
    [],
  );
  const startConversation = useCallback(
    (kind: ChatKind, node: GraphNode | null, projectTitle: string): string => {
      const chatId = window.crypto.randomUUID();
      setDraftConversations((current) => [
        { chatId, kind, nodeId: node?.id ?? null, title: node?.title ?? projectTitle },
        ...current,
      ]);
      return chatId;
    },
    [],
  );
  const discardDraft = useCallback(
    (chatId: string) => {
      setDraftConversations((current) => current.filter((draft) => draft.chatId !== chatId));
      if (selectedChatId === chatId) selectChat(null);
    },
    [selectedChatId, selectChat],
  );
  const ensureConversation = useCallback(
    (
      conversations: ChatConversation[],
      kind: ChatKind,
      node: GraphNode | null,
      projectTitle: string,
    ): string => {
      return (
        latestConversation(conversations, kind, node?.id ?? null, graphTarget)?.chatId ??
        startConversation(kind, node, projectTitle)
      );
    },
    [graphTarget, startConversation],
  );
  const refreshChatSummaries = useCallback(
    async (requestedProjectId: string, base: string) => {
      const id = store.register(requestedProjectId, chatsResourceProfile(requestedProjectId, base));
      store.invalidate(id);
    },
    [store],
  );
  const refreshChatReads = useCallback(async () => {
    if (projectId) store.invalidate(resourceKeyId(chatsResourceKey(projectId)));
  }, [store, projectId]);
  const markVisibleChatRead = useCallback(
    (tasks: AgentTask[], visibleChatId: string | null) => {
      if (!projectId || !visibleChatId || !chatReads) return;
      const readThrough = chatReadThrough(tasks, chatReads, visibleChatId);
      const marker = chatReads.reads[visibleChatId] ?? chatReads.baseline;
      if (!readThrough || Date.parse(readThrough) <= Date.parse(marker)) return;
      const id = resourceKeyId(chatsResourceKey(projectId));
      const mutation = store.beginMutation(
        crypto.randomUUID(),
        new Map([
          [
            id,
            (value: unknown) => {
              const current = value as ChatsResource;
              return {
                ...current,
                reads: {
                  ...current.reads,
                  reads: { ...current.reads.reads, [visibleChatId]: readThrough },
                },
              };
            },
          ],
        ]),
      );
      void markChatRead(
        `${apiBase}/chats/${encodeURIComponent(visibleChatId)}/read?inventory=true`,
        readThrough,
      )
        .then((reads) => {
          const current = store.view<ChatsResource>(id).value;
          mutation.settle(current ? new Map([[id, { ...current, reads }]]) : undefined);
        })
        .catch((error) => {
          mutation.fail();
          reportError(
            `Chat could not be marked read: ${error instanceof Error ? error.message : String(error)}`,
          );
        });
    },
    [apiBase, chatReads, projectId, reportError, store],
  );
  const resetProjectChats = useCallback(() => {
    setFloatingChat(null);
    setDraftConversations([]);
    selectChat(null);
  }, [selectChat]);
  const restoreProjectChats = useCallback(
    (snapshot: ChatUiState, _nodes?: Record<string, GraphNode>) => {
      setFloatingChat(snapshot.floatingChat);
      setDraftConversations([...snapshot.draftConversations]);
      selectChat(snapshot.selectedChatId);
    },
    [selectChat],
  );
  const uiSnapshot: ChatUiState = { floatingChat, draftConversations, selectedChatId };
  const snapshot: ChatStateSnapshot = {
    ...uiSnapshot,
    chatReads,
    chatSummaries,
    inventoryLoaded: !!inventory.value,
    chatSummaryTotal: chatSummaries.length,
    chatSummaryNextOffset: chatSummaries.length,
    chatTranscripts,
    selectedCanonicalChat,
  };
  return {
    snapshot,
    uiSnapshot,
    chatSummariesLoading: inventory.loading,
    visibleChatSummaries,
    inventoryChatSummaries,
    inventoryLoaded: !!inventory.value,
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
  };
}
