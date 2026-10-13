import { useRefreshRuntime, useResource } from "../core/useResource";
import { resourceKeyId } from "../core/resourceStore";
import { experimentIndexProfile } from "../experiments/episodeResources";
import { graphSessionKey, graphTargetUrl, MAIN_GRAPH } from "../core/graphTarget";
import type { GraphTargetRef } from "../core/types";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, loadSpaceRuns } from "../core/api";
import { experimentBoardHref } from "../experiments/experimentBoardModel";
import {
  adjacentProjectTabId,
  closeProjectTab,
  openProjectTab,
  type ProjectTab,
} from "./projectTabs";
import type {
  Episode,
  ExperimentLoopIndex,
  ExperimentLoopIndexEntry,
  ProjectCard,
  ProjectSnapshot,
  SpaceRunIndexEntry,
} from "../core/types";

const PROJECT_HEADER_COLLAPSED_KEY = "rcp:project-header-collapsed";
export const EXPERIMENT_BOARD_POLL_DELAY_MS = 5_000;
interface UseProjectTabsOptions {
  initialProjectId: string | null;
  initialSetupOpen: boolean;
  projectIndexReady: boolean;
  project: ProjectSnapshot | null;
  reportError: (message: string) => void;
}

export function cacheProjectTabState<T>(cache: Map<string, T>, projectId: string, state: T): void {
  cache.delete(projectId);
  cache.set(projectId, state);
}

/** The route that reopens a project on the graph target its human last left it on. */
export function projectReturnHash(
  leftGraphTargets: ReadonlyMap<string, GraphTargetRef>,
  projectId: string,
): string {
  return graphTargetUrl(
    `/projects/${encodeURIComponent(projectId)}`,
    leftGraphTargets.get(projectId) ?? MAIN_GRAPH,
  );
}

export function mergeProjectExperimentLoops(
  current: ExperimentLoopIndexEntry[],
  projectId: string,
  nextEntries: ExperimentLoopIndexEntry[],
): ExperimentLoopIndexEntry[] {
  return [...current.filter((entry) => entry.project_id !== projectId), ...nextEntries];
}

export function experimentLoopRefreshIsCurrent(
  responseGeneration: number,
  currentGeneration: number,
): boolean {
  return responseGeneration === currentGeneration;
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

export function useProjectTabs<
  T extends { projectId: string | null; graphTarget: GraphTargetRef },
>({
  initialProjectId,
  initialSetupOpen,
  projectIndexReady,
  project,
  reportError,
}: UseProjectTabsOptions) {
  const [projectId, setProjectId] = useState<string | null>(initialProjectId);
  const [setupOpen, setSetupOpen] = useState(initialSetupOpen);
  const [projects, setProjects] = useState<ProjectCard[]>([]);
  const [openProjectTabs, setOpenProjectTabs] = useState<ProjectTab[]>([]);
  const { store } = useRefreshRuntime();
  const experimentIndex = useResource(
    `${projectId}:experiment-index-view`,
    projectIndexReady && projectId ? experimentIndexProfile(projectId) : null,
  );
  const experimentLoops = useMemo(
    () => experimentIndex.value?.entries ?? [],
    [experimentIndex.value],
  );
  const unavailableExperimentLoops = experimentIndex.value?.unavailable ?? [];
  const [spaceRuns, setSpaceRuns] = useState<SpaceRunIndexEntry[]>([]);
  const [projectHeaderCollapsed, setProjectHeaderCollapsed] = useState(() =>
    readProjectHeaderCollapsed(initialProjectId),
  );
  const activeProjectId = useRef(projectId);
  const openProjectTabsRef = useRef(openProjectTabs);
  const projectTabStatesRef = useRef(new Map<string, { state: T }>());
  // Where the human left each project; background cache writes never change it.
  const leftGraphTargetsRef = useRef(new Map<string, GraphTargetRef>());
  const spaceRunRefreshGeneration = useRef(0);
  activeProjectId.current = projectId;
  openProjectTabsRef.current = openProjectTabs;

  const isActiveProject = useCallback(
    (requestedProjectId: string) => activeProjectId.current === requestedProjectId,
    [],
  );
  const getActiveProjectId = useCallback(() => activeProjectId.current, []);
  const replaceProjects = useCallback((nextProjects: ProjectCard[]) => {
    setProjects(nextProjects);
  }, []);
  const refreshProjectExperimentLoops = useCallback(
    async (requestedProjectId: string) => {
      const id = resourceKeyId(experimentIndexProfile(requestedProjectId).key);
      store.invalidate(id);
      return store.view<ExperimentLoopIndex>(id).value?.entries ?? [];
    },
    [store],
  );
  const refreshExperimentLoops = useCallback(async () => {
    const requestedProjectId = activeProjectId.current;
    return requestedProjectId ? refreshProjectExperimentLoops(requestedProjectId) : [];
  }, [refreshProjectExperimentLoops]);
  const refreshSpaceRuns = useCallback(async () => {
    const generation = ++spaceRunRefreshGeneration.current;
    const nextEntries = await loadSpaceRuns();
    if (generation === spaceRunRefreshGeneration.current) setSpaceRuns(nextEntries);
  }, []);
  const replaceEpisodeRunArchive = useCallback(
    (episode: Episode) => {
      spaceRunRefreshGeneration.current += 1;
      const archive = { archived: episode.archived, can_archive: episode.can_archive };
      const id = resourceKeyId(experimentIndexProfile(episode.project_id).key);
      const current = store.view<ExperimentLoopIndex>(id).value;
      if (current)
        store.write(id, {
          ...current,
          entries: current.entries.map((entry) =>
            entry.episode.episode_id === episode.episode_id
              ? {
                  ...entry,
                  episode: { ...entry.episode, ...archive },
                  control: {
                    ...entry.control,
                    episode: entry.control.episode
                      ? { ...entry.control.episode, ...archive }
                      : null,
                  },
                }
              : entry,
          ),
        });
      setSpaceRuns((current) =>
        current.map((entry) =>
          entry.project_id === episode.project_id && entry.episode_id === episode.episode_id
            ? { ...entry, ...archive }
            : entry,
        ),
      );
    },
    [store],
  );
  const loadProjectIndex = useCallback(async () => {
    setProjects(await api<ProjectCard[]>("/api/projects"));
  }, []);
  const applyHashRoute = useCallback((nextProjectId: string | null, nextSetupOpen: boolean) => {
    setSetupOpen(nextSetupOpen);
    setProjectId(nextProjectId);
  }, []);
  const openSetup = useCallback(() => {
    setSetupOpen(true);
    setProjectId(null);
    window.location.hash = "/projects/new";
  }, []);
  const returnToProjects = useCallback(() => {
    setSetupOpen(false);
    setProjectId(null);
    window.location.hash = "";
  }, []);

  const setTabs = useCallback((nextTabs: ProjectTab[]) => {
    openProjectTabsRef.current = nextTabs;
    setOpenProjectTabs(nextTabs);
  }, []);
  const tabForProject = useCallback(
    (id: string): ProjectTab => ({
      id,
      name:
        projects.find((item) => item.id === id)?.name ??
        spaceRuns.find((item) => item.project_id === id)?.project_name ??
        experimentLoops.find((item) => item.project_id === id)?.project_name ??
        (project?.id === id ? project.name : id),
    }),
    [experimentLoops, project, projects, spaceRuns],
  );
  const commitProjectOpen = useCallback(
    (id: string, experimentRoute: string | null = null) => {
      setTabs(openProjectTab(openProjectTabsRef.current, tabForProject(id)));
      setSetupOpen(false);
      window.location.hash = experimentRoute
        ? experimentBoardHref(id, experimentRoute).slice(1)
        : projectReturnHash(leftGraphTargetsRef.current, id);
    },
    [setTabs, tabForProject],
  );
  const activateProjectTab = useCallback(
    (id: string) => {
      if (id === activeProjectId.current) return;
      commitProjectOpen(id);
    },
    [commitProjectOpen],
  );
  const closeDockedProject = useCallback(
    (id: string): boolean => {
      const result = closeProjectTab(openProjectTabsRef.current, activeProjectId.current, id);
      if (result.tabs === openProjectTabsRef.current) return false;
      for (const [key, state] of projectTabStatesRef.current) {
        if (state.state.projectId === id) projectTabStatesRef.current.delete(key);
      }
      leftGraphTargetsRef.current.delete(id);
      setTabs(result.tabs);
      if (id !== activeProjectId.current) return true;
      if (result.activeProjectId) {
        setSetupOpen(false);
        window.location.hash = projectReturnHash(
          leftGraphTargetsRef.current,
          result.activeProjectId,
        );
      } else {
        setSetupOpen(false);
        setProjectId(null);
        window.location.hash = "";
      }
      return true;
    },
    [setTabs],
  );
  const removeProject = useCallback(
    (id: string) => {
      setProjects((current) => current.filter((item) => item.id !== id));
      setSpaceRuns((current) => current.filter((item) => item.project_id !== id));
      for (const [key, state] of projectTabStatesRef.current) {
        if (state.state.projectId === id) projectTabStatesRef.current.delete(key);
      }
      leftGraphTargetsRef.current.delete(id);
      setTabs(closeProjectTab(openProjectTabsRef.current, activeProjectId.current, id).tabs);
    },
    [setTabs],
  );

  const resetProjectHeader = useCallback((nextProjectId: string | null) => {
    setProjectHeaderCollapsed(readProjectHeaderCollapsed(nextProjectId));
  }, []);
  const restoreProjectHeader = useCallback((collapsed: boolean) => {
    setProjectHeaderCollapsed(collapsed);
  }, []);
  const toggleProjectHeader = useCallback(() => {
    setProjectHeaderCollapsed((collapsed) => !collapsed);
  }, []);

  const cacheProjectState = useCallback((id: string, state: T) => {
    projectTabStatesRef.current.set(graphSessionKey(id, state.graphTarget), { state });
  }, []);
  const clearProjectCache = useCallback(() => projectTabStatesRef.current.clear(), []);
  const rememberLeftGraphTarget = useCallback((id: string, target: GraphTargetRef) => {
    leftGraphTargetsRef.current.set(id, target);
  }, []);
  const cachedProjectStateForOpen = useCallback(
    (id: string, graphTarget: GraphTargetRef = MAIN_GRAPH) => {
      const key = graphSessionKey(id, graphTarget);
      const cache = projectTabStatesRef.current;
      const cached = cache.get(key);
      if (!cached?.state) return null;
      cache.delete(key);
      cache.set(key, cached);
      return { state: cached.state, loading: false as const };
    },
    [],
  );
  const inactiveCachedProjectState = useCallback(
    (id: string) =>
      inactiveProjectTabState(
        projectTabStatesRef.current,
        openProjectTabsRef.current,
        activeProjectId.current,
        id,
      )?.state ?? null,
    [],
  );
  const isProjectTabOpen = useCallback(
    (id: string) => openProjectTabsRef.current.some((tab) => tab.id === id),
    [],
  );
  const adjacentProjectId = useCallback(
    (offset: -1 | 1) =>
      adjacentProjectTabId(openProjectTabsRef.current, activeProjectId.current, offset),
    [],
  );

  useEffect(() => {
    if (!project || project.id !== projectId) return;
    const nextTabs = openProjectTab(openProjectTabsRef.current, {
      id: project.id,
      name: project.name,
    });
    if (nextTabs === openProjectTabsRef.current) return;
    setTabs(nextTabs);
  }, [project, projectId, setTabs]);

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
    if (!projectIndexReady || projectId || setupOpen) return;
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
        await refreshSpaceRuns();
      } catch (error) {
        if (!stopped) {
          reportError(
            `Runs could not be refreshed: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      }
      if (!stopped) schedule();
    };
    void poll();
    return () => {
      stopped = true;
      spaceRunRefreshGeneration.current += 1;
      window.clearTimeout(timer);
    };
  }, [projectIndexReady, projectId, refreshSpaceRuns, reportError, setupOpen]);

  return {
    projectId,
    setupOpen,
    projects,
    openProjectTabs,
    experimentLoops,
    experimentLoopsUnavailableRows: unavailableExperimentLoops.filter(
      (entry) => entry.project_id === projectId,
    ),
    experimentLoopsLoaded: projectId !== null && !experimentIndex.loading,
    experimentLoopsUnavailable: projectId !== null && experimentIndex.stale,
    spaceRuns,
    projectHeaderCollapsed,
    isActiveProject,
    getActiveProjectId,
    replaceProjects,
    loadProjectIndex,
    refreshExperimentLoops,
    refreshProjectExperimentLoops,
    refreshSpaceRuns,
    replaceEpisodeRunArchive,
    applyHashRoute,
    openSetup,
    returnToProjects,
    commitProjectOpen,
    activateProjectTab,
    closeDockedProject,
    removeProject,
    resetProjectHeader,
    restoreProjectHeader,
    toggleProjectHeader,
    cacheProjectState,
    clearProjectCache,
    rememberLeftGraphTarget,
    cachedProjectStateForOpen,
    inactiveCachedProjectState,
    isProjectTabOpen,
    adjacentProjectId,
  };
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

function pageIsHidden(): boolean {
  if (typeof document === "undefined") return false;
  return document.visibilityState === "hidden";
}
