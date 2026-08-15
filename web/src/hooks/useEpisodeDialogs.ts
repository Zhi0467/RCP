import { useCallback, useEffect, useRef, useState } from "react";
import { keepResultView, loadEpisodeMessages, loadEpisodes, loadResultViews } from "../api";
import { isLiveEpisode, mergeEpisode } from "../campaigns";
import type { Episode, EpisodeMessage, ResultViewDescriptor } from "../types";

export const LIVE_EPISODE_POLL_INTERVAL_MS = 1_500;

interface LiveEpisodePollingClock {
  setTimeout(callback: () => void, delay: number): number;
  clearTimeout(timeoutId: number): void;
}

export function startLiveEpisodePolling(
  clock: LiveEpisodePollingClock,
  refresh: () => Promise<void>,
  onError: (error: unknown) => void,
  onSuccess: () => void,
): () => void {
  let stopped = false;
  let timeoutId = 0;
  const schedule = () => {
    timeoutId = clock.setTimeout(() => void poll(), LIVE_EPISODE_POLL_INTERVAL_MS);
  };
  const poll = async () => {
    try {
      await refresh();
      if (!stopped) onSuccess();
    } catch (error) {
      if (!stopped) onError(error);
    } finally {
      if (!stopped) schedule();
    }
  };
  schedule();
  return () => {
    stopped = true;
    clock.clearTimeout(timeoutId);
  };
}

export function resultViewSelectionKey(
  projectId: string | null,
  experimentId: string | null,
  chatId: string | null,
): string | null {
  return projectId && experimentId && chatId
    ? JSON.stringify([projectId, experimentId, chatId])
    : null;
}

export function resultViewSelectionIsCurrent(
  expectedKey: string | null,
  expectedGeneration: number,
  currentKey: string | null,
  currentGeneration: number,
): boolean {
  return (
    expectedKey !== null && expectedKey === currentKey && expectedGeneration === currentGeneration
  );
}

export function resultViewLoadIsCurrent(
  expectedKey: string | null,
  expectedSelectionGeneration: number,
  expectedLoadGeneration: number,
  currentKey: string | null,
  currentSelectionGeneration: number,
  currentLoadGeneration: number,
): boolean {
  return (
    resultViewSelectionIsCurrent(
      expectedKey,
      expectedSelectionGeneration,
      currentKey,
      currentSelectionGeneration,
    ) && expectedLoadGeneration === currentLoadGeneration
  );
}

interface EpisodeState {
  projectId: string | null;
  episodes: Episode[];
  messages: Record<string, EpisodeMessage[]>;
}

interface ResultViewState {
  projectId: string | null;
  experimentId: string | null;
  chatId: string | null;
  views: ResultViewDescriptor[];
  authoritative: boolean;
  error: string | null;
}

interface UseEpisodeDialogsOptions {
  projectId: string | null;
  apiBase: string;
  selectedExperimentId: string | null;
  selectedExperimentChatId: string | null;
  isActiveProject: (projectId: string) => boolean;
}

export function useEpisodeDialogs({
  projectId,
  apiBase,
  selectedExperimentId,
  selectedExperimentChatId,
  isActiveProject,
}: UseEpisodeDialogsOptions) {
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [autoResearchDialogOpen, setAutoResearchDialogOpen] = useState(false);
  const [autoResearchStartError, setAutoResearchStartError] = useState<string | null>(null);
  const [episodeAction, setEpisodeAction] = useState<string | null>(null);
  const [episodeRefreshError, setEpisodeRefreshError] = useState<string | null>(null);
  const [episodeState, setEpisodeState] = useState<EpisodeState>({
    projectId: null,
    episodes: [],
    messages: {},
  });
  const [resultViewState, setResultViewState] = useState<ResultViewState>({
    projectId: null,
    experimentId: null,
    chatId: null,
    views: [],
    authoritative: false,
    error: null,
  });
  const resultViewLoadGeneration = useRef(0);
  const resultViewSelectionRef = useRef<{ key: string | null; generation: number }>({
    key: null,
    generation: 0,
  });

  const episodes = episodeState.projectId === projectId ? episodeState.episodes : [];
  const episodeMessages = episodeState.projectId === projectId ? episodeState.messages : {};
  const liveAutoResearchEpisode = episodes.find(isLiveEpisode) ?? null;

  const refreshEpisodes = useCallback(async () => {
    if (!projectId || !apiBase) return;
    const requestedProjectId = projectId;
    const nextEpisodes = await loadEpisodes(apiBase, "auto_research");
    if (!isActiveProject(requestedProjectId)) return;
    setEpisodeState((current) => ({
      projectId: requestedProjectId,
      episodes: nextEpisodes,
      messages: current.projectId === requestedProjectId ? current.messages : {},
    }));
  }, [apiBase, projectId]);

  const refreshEpisodeMessages = useCallback(
    async (episodeId: string) => {
      if (!projectId || !apiBase) return;
      const requestedProjectId = projectId;
      const nextMessages = await loadEpisodeMessages(apiBase, episodeId);
      if (!isActiveProject(requestedProjectId)) return;
      setEpisodeState((current) =>
        current.projectId === requestedProjectId
          ? {
              ...current,
              messages: { ...current.messages, [episodeId]: nextMessages },
            }
          : current,
      );
    },
    [apiBase, projectId],
  );

  useEffect(() => {
    if (!projectId || !apiBase) {
      setEpisodeRefreshError(null);
      setEpisodeState({ projectId: null, episodes: [], messages: {} });
      return;
    }
    const requestedProjectId = projectId;
    setEpisodeRefreshError(null);
    setEpisodeState((current) =>
      current.projectId === requestedProjectId
        ? current
        : { projectId: requestedProjectId, episodes: [], messages: {} },
    );
    void refreshEpisodes()
      .then(() => {
        if (isActiveProject(requestedProjectId)) setEpisodeRefreshError(null);
      })
      .catch((error) => {
        if (!isActiveProject(requestedProjectId)) return;
        setEpisodeRefreshError(
          `Auto-research could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
        );
      });
  }, [apiBase, projectId, refreshEpisodes]);

  useEffect(() => {
    const episodeId = liveAutoResearchEpisode?.episode_id;
    if (!episodeId) return;
    return startLiveEpisodePolling(
      {
        setTimeout: (callback, delay) => window.setTimeout(callback, delay),
        clearTimeout: (timeoutId) => window.clearTimeout(timeoutId),
      },
      async () => {
        await Promise.all([refreshEpisodes(), refreshEpisodeMessages(episodeId)]);
      },
      (error) => {
        setEpisodeRefreshError(
          `Auto-research could not refresh: ${error instanceof Error ? error.message : String(error)}`,
        );
      },
      () => setEpisodeRefreshError(null),
    );
  }, [liveAutoResearchEpisode?.episode_id, refreshEpisodeMessages, refreshEpisodes]);

  const replaceEpisode = useCallback(
    (nextEpisode: Episode) => {
      setEpisodeState((current) => {
        if (!isActiveProject(nextEpisode.project_id)) return current;
        const currentEpisodes =
          current.projectId === nextEpisode.project_id ? current.episodes : [];
        return {
          projectId: nextEpisode.project_id,
          messages: current.projectId === nextEpisode.project_id ? current.messages : {},
          episodes: mergeEpisode(currentEpisodes, nextEpisode),
        };
      });
    },
    [isActiveProject],
  );

  const recordEpisodeMessage = useCallback(
    (requestedProjectId: string, episodeId: string, saved: EpisodeMessage) => {
      setEpisodeState((current) =>
        current.projectId === requestedProjectId
          ? {
              ...current,
              messages: {
                ...current.messages,
                [episodeId]: [
                  ...(current.messages[episodeId] ?? []).filter(
                    (item) => item.message_id !== saved.message_id,
                  ),
                  saved,
                ],
              },
            }
          : current,
      );
    },
    [],
  );

  const openRunDialog = useCallback(() => setRunDialogOpen(true), []);
  const closeRunDialog = useCallback(() => setRunDialogOpen(false), []);
  const openAutoResearchDialog = useCallback(() => {
    setAutoResearchStartError(null);
    setAutoResearchDialogOpen(true);
  }, []);
  const closeAutoResearchDialog = useCallback(() => setAutoResearchDialogOpen(false), []);
  const reportAutoResearchStartError = useCallback(
    (message: string | null) => setAutoResearchStartError(message),
    [],
  );
  const beginEpisodeAction = useCallback(
    (action: string) => {
      if (episodeAction) return null;
      setEpisodeAction(action);
      return () => setEpisodeAction(null);
    },
    [episodeAction],
  );

  const selectedResultViewKey = resultViewSelectionKey(
    projectId,
    selectedExperimentId,
    selectedExperimentChatId,
  );
  if (resultViewSelectionRef.current.key !== selectedResultViewKey) {
    resultViewSelectionRef.current = {
      key: selectedResultViewKey,
      generation: resultViewSelectionRef.current.generation + 1,
    };
  }

  const refreshResultViews = useCallback(async () => {
    const requestedProjectId = projectId;
    const experimentId = selectedExperimentId;
    const chatId = selectedExperimentChatId;
    const selectionKey = resultViewSelectionKey(requestedProjectId, experimentId, chatId);
    const selectionGeneration = resultViewSelectionRef.current.generation;
    const loadGeneration = ++resultViewLoadGeneration.current;
    if (!requestedProjectId || !apiBase || !experimentId || !chatId || !selectionKey) {
      setResultViewState({
        projectId: null,
        experimentId: null,
        chatId: null,
        views: [],
        authoritative: false,
        error: null,
      });
      return;
    }
    setResultViewState((current) =>
      current.projectId === requestedProjectId &&
      current.experimentId === experimentId &&
      current.chatId === chatId
        ? { ...current, error: null }
        : {
            projectId: requestedProjectId,
            experimentId,
            chatId,
            views: [],
            authoritative: false,
            error: null,
          },
    );
    try {
      const descriptors = await loadResultViews(apiBase, experimentId, chatId);
      if (
        !isActiveProject(requestedProjectId) ||
        !resultViewLoadIsCurrent(
          selectionKey,
          selectionGeneration,
          loadGeneration,
          resultViewSelectionRef.current.key,
          resultViewSelectionRef.current.generation,
          resultViewLoadGeneration.current,
        )
      )
        return;
      setResultViewState((current) =>
        current.projectId === requestedProjectId &&
        current.experimentId === experimentId &&
        current.chatId === chatId
          ? {
              ...current,
              views: descriptors.filter(
                (descriptor) =>
                  descriptor.experiment_id === experimentId && descriptor.chat_id === chatId,
              ),
              authoritative: true,
              error: null,
            }
          : current,
      );
    } catch (error) {
      if (
        !isActiveProject(requestedProjectId) ||
        !resultViewLoadIsCurrent(
          selectionKey,
          selectionGeneration,
          loadGeneration,
          resultViewSelectionRef.current.key,
          resultViewSelectionRef.current.generation,
          resultViewLoadGeneration.current,
        )
      )
        return;
      setResultViewState((current) =>
        current.projectId === requestedProjectId &&
        current.experimentId === experimentId &&
        current.chatId === chatId
          ? {
              ...current,
              error: `Result views could not be loaded: ${error instanceof Error ? error.message : String(error)}`,
            }
          : current,
      );
    }
  }, [apiBase, projectId, selectedExperimentChatId, selectedExperimentId]);

  const keepSelectedResultView = useCallback(
    async (viewId: string) => {
      const requestedProjectId = projectId;
      const experimentId = selectedExperimentId;
      const chatId = selectedExperimentChatId;
      const selectionKey = resultViewSelectionKey(requestedProjectId, experimentId, chatId);
      const selectionGeneration = resultViewSelectionRef.current.generation;
      const requestedApiBase = apiBase;
      if (!requestedProjectId || !requestedApiBase || !experimentId || !chatId || !selectionKey) {
        throw new Error("This run conversation is no longer selected.");
      }
      const kept = await keepResultView(requestedApiBase, viewId);
      if (
        kept.view_id !== viewId ||
        kept.experiment_id !== experimentId ||
        kept.chat_id !== chatId
      ) {
        throw new Error("Keep returned a result view outside the selected run conversation.");
      }
      if (
        !resultViewSelectionIsCurrent(
          selectionKey,
          selectionGeneration,
          resultViewSelectionRef.current.key,
          resultViewSelectionRef.current.generation,
        )
      )
        return;
      resultViewLoadGeneration.current += 1;
      setResultViewState((current) =>
        current.projectId === requestedProjectId &&
        current.experimentId === experimentId &&
        current.chatId === chatId
          ? {
              ...current,
              views: current.views.map((view) => (view.view_id === kept.view_id ? kept : view)),
            }
          : current,
      );
    },
    [apiBase, projectId, selectedExperimentChatId, selectedExperimentId],
  );

  const selectedResultViews =
    resultViewState.projectId === projectId &&
    resultViewState.experimentId === selectedExperimentId &&
    resultViewState.chatId === selectedExperimentChatId &&
    resultViewState.authoritative
      ? resultViewState.views
      : undefined;
  const selectedResultViewsError =
    resultViewState.projectId === projectId &&
    resultViewState.experimentId === selectedExperimentId &&
    resultViewState.chatId === selectedExperimentChatId
      ? resultViewState.error
      : null;

  return {
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
    refreshResultViews,
    keepSelectedResultView,
  };
}
