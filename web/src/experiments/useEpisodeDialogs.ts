import { useCallback, useState } from "react";
import { isLiveEpisode, mergeEpisode } from "./campaigns";
import type { Episode, EpisodeMessage } from "../core/types";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime, useResource } from "../core/useResource";
import {
  episodeProfile,
  episodeResourceKey,
  episodesProfile,
  type EpisodeResource,
} from "./episodeResources";

interface UseEpisodeDialogsOptions {
  projectId: string | null;
  apiBase: string;
  selectedAutoResearchEpisodeId: string | null;
  isActiveProject: (projectId: string) => boolean;
  runsVisible?: boolean;
}

export function useEpisodeDialogs({
  projectId,
  selectedAutoResearchEpisodeId,
  runsVisible = false,
}: UseEpisodeDialogsOptions) {
  const { store } = useRefreshRuntime();
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [autoResearchDialogOpen, setAutoResearchDialogOpen] = useState(false);
  const [autoResearchStartError, setAutoResearchStartError] = useState<string | null>(null);
  const [episodeAction, setEpisodeAction] = useState<string | null>(null);
  const profile = projectId ? episodesProfile(projectId) : null;
  const resource = useResource(`${projectId}:episodes-view`, profile);
  const selected = useResource(
    `${projectId}:episodes-view`,
    projectId && runsVisible && selectedAutoResearchEpisodeId
      ? episodeProfile(projectId, selectedAutoResearchEpisodeId)
      : null,
  );
  const episodes = selected.value?.episode
    ? mergeEpisode(resource.value ?? [], selected.value.episode)
    : (resource.value ?? []);
  const episodeMessages =
    selectedAutoResearchEpisodeId && selected.value
      ? { [selectedAutoResearchEpisodeId]: selected.value.messages }
      : {};
  const episodeRefreshError = resource.error ? String(resource.error) : null;
  const liveAutoResearchEpisode =
    episodes.find((episode) => episode.mode === "auto_research" && isLiveEpisode(episode)) ?? null;
  const refreshEpisodes = useCallback(async () => {
    if (!projectId) return false;
    // Callers hold their action lock until this read settles or fails.
    await store.refresh(resourceKeyId(episodesProfile(projectId).key));
    return true;
  }, [projectId, store]);
  const refreshEpisodeMessages = useCallback(
    async (episodeId: string) => {
      if (projectId) store.invalidate(resourceKeyId(episodeResourceKey(projectId, episodeId)));
    },
    [projectId, store],
  );
  const replaceEpisode = useCallback(
    (nextEpisode: Episode) => {
      const id = resourceKeyId(episodesProfile(nextEpisode.project_id).key);
      store.write(id, mergeEpisode(store.view<Episode[]>(id).value ?? [], nextEpisode));
      store.invalidate(
        resourceKeyId(episodeResourceKey(nextEpisode.project_id, nextEpisode.episode_id)),
      );
    },
    [store],
  );
  const recordEpisodeMessage = useCallback(
    (requestedProjectId: string, episodeId: string, saved: EpisodeMessage) => {
      const id = resourceKeyId(episodeResourceKey(requestedProjectId, episodeId));
      const current = store.view<EpisodeResource>(id).value;
      if (current)
        store.write(id, {
          ...current,
          messages: [
            ...current.messages.filter((item) => item.message_id !== saved.message_id),
            saved,
          ],
        });
      store.invalidate(id);
    },
    [store],
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

  return {
    runDialogOpen,
    autoResearchDialogOpen,
    autoResearchStartError,
    episodeAction,
    episodeRefreshError,
    episodes,
    episodeMessages,
    liveAutoResearchEpisode,
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
  };
}
