import { api } from "../core/api";
import type { ResourceKey, ResourceProfile } from "../core/resourceStore";
import type {
  Episode,
  EpisodeMessage,
  EpisodeTimelineResponse,
  ExperimentLoopIndex,
  GraphTargetRef,
  WatcherRecord,
} from "../core/types";
import { graphTargetUrl } from "../core/graphTarget";

const projectBase = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}`;
const key = (project: string, name: string): ResourceKey => ({
  backend: "",
  member: "",
  project,
  name,
});

export function episodesProfile(projectId: string): ResourceProfile<Episode[]> {
  return {
    key: key(projectId, "episodes"),
    fetch: (signal) => api(`${projectBase(projectId)}/episodes`, { signal }),
  };
}

export function experimentIndexProfile(projectId: string): ResourceProfile<ExperimentLoopIndex> {
  return {
    key: { ...key(projectId, "episodes"), query: "experiment-index" },
    fetch: (signal) =>
      api(`${projectBase(projectId)}/experiment-episodes?mode=experiment_loop`, { signal }),
  };
}

export interface EpisodeResource {
  episode: Episode | null;
  messages: EpisodeMessage[];
  timeline: EpisodeTimelineResponse;
}

export function episodeResourceKey(projectId: string, episodeId: string): ResourceKey {
  return { ...key(projectId, "episode"), entity: episodeId };
}

export function episodeProfile(
  projectId: string,
  episodeId: string,
): ResourceProfile<EpisodeResource> {
  const base = `${projectBase(projectId)}/episodes/${encodeURIComponent(episodeId)}`;
  return {
    watch: [{ name: "chats" }],
    key: episodeResourceKey(projectId, episodeId),
    fetch: async (signal) => {
      const [episodes, messages, timeline] = await Promise.all([
        api<Episode[]>(
          `${projectBase(projectId)}/episodes?episode_id=${encodeURIComponent(episodeId)}`,
          { signal },
        ),
        api<EpisodeMessage[]>(`${base}/messages`, { signal }),
        api<EpisodeTimelineResponse>(`${base}/timeline`, { signal }),
      ]);
      return {
        episode: episodes.find((item) => item.episode_id === episodeId) ?? null,
        messages,
        timeline,
      };
    },
  };
}

export function watcherResourceKey(projectId: string, target: GraphTargetRef): ResourceKey {
  return {
    ...key(projectId, "watchers"),
    query: target.kind === "main" ? "main" : `branch:${target.branch_id}`,
  };
}

export function watchersProfile(
  projectId: string,
  target: GraphTargetRef,
  { ownTargetOnly = false } = {},
): ResourceProfile<WatcherRecord[]> {
  // Capture the route now: a mutable view target never decides where a settled read lands.
  // A branch view lists every target's watchers; ownTargetOnly reads just its own.
  const base = `${projectBase(projectId)}/watchers`;
  const path =
    target.kind === "main"
      ? base
      : graphTargetUrl(ownTargetOnly ? base : `${base}?all_targets=true`, target);
  const resourceKey = watcherResourceKey(projectId, target);
  return {
    key: ownTargetOnly ? { ...resourceKey, query: `${resourceKey.query}:own` } : resourceKey,
    fetch: (signal) => api(path, { signal }),
  };
}
