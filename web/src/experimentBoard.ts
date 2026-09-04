import type {
  AgentTask,
  AppView,
  ExperimentControlState,
  ExperimentLoopIndexEntry,
  GraphNode,
  GraphTargetRef,
  SpaceRunIndexEntry,
  WatcherRecord,
} from "./types";

export interface ProjectHashRoute {
  projectId: string | null;
  view: AppView;
  projectViewSpecified: boolean;
  experimentId: string | null;
  experimentRoute: ExperimentRouteIdentity | null;
  autoResearchEpisodeId: string | null;
}

export interface ExperimentRouteIdentity {
  experiment_id: string;
  episode_id: string;
  graph_target: GraphTargetRef;
  parent_episode_id: string | null;
}

export interface ExperimentExecutionProjection {
  nodes: GraphNode[];
  tasks: AgentTask[];
  watchers: WatcherRecord[];
  experimentControl: Record<string, ExperimentControlState>;
  exactBranchEntry: ExperimentLoopIndexEntry | null;
  staleMainRoute: ExperimentRouteIdentity | null;
}

const INDEX_ROUTE_PREFIX = "rcp-index:";
const AUTO_RESEARCH_ROUTE_PREFIX = "rcp-auto-research:";

export function experimentTerminalLabel(status: unknown): string {
  if (status === "completed") return "Succeeded";
  if (status === "abandoned") return "Abandoned";
  if (status === "superseded") return "Superseded";
  return "Completed";
}

export function experimentBoardRouteToken(entry: ExperimentLoopIndexEntry): string {
  return `${INDEX_ROUTE_PREFIX}${JSON.stringify(experimentRouteIdentity(entry))}`;
}

export function projectRunsNeedsExperimentIndex(projectId: string | null, view: AppView): boolean {
  return Boolean(projectId && view === "execution");
}

export function spaceRunRouteToken(entry: SpaceRunIndexEntry): string {
  if (entry.mode === "auto_research") {
    return `${AUTO_RESEARCH_ROUTE_PREFIX}${entry.episode_id}`;
  }
  if (!entry.experiment_id) return "";
  return `${INDEX_ROUTE_PREFIX}${JSON.stringify({
    experiment_id: entry.experiment_id,
    episode_id: entry.episode_id,
    graph_target: entry.graph_target,
    parent_episode_id: entry.parent_episode_id,
  })}`;
}

export function experimentBoardHref(
  projectId: string,
  experimentSelection: string | ExperimentRouteIdentity,
): string {
  if (
    typeof experimentSelection === "string" &&
    experimentSelection.startsWith(AUTO_RESEARCH_ROUTE_PREFIX)
  ) {
    const episodeId = experimentSelection.slice(AUTO_RESEARCH_ROUTE_PREFIX.length);
    return episodeId
      ? `#/projects/${encodeURIComponent(projectId)}?view=runs&mode=auto_research&episode=${encodeURIComponent(episodeId)}`
      : `#/projects/${encodeURIComponent(projectId)}?view=runs`;
  }
  const route =
    typeof experimentSelection === "string"
      ? experimentRouteFromToken(experimentSelection)
      : experimentSelection;
  if (
    typeof experimentSelection === "string" &&
    experimentSelection.startsWith(INDEX_ROUTE_PREFIX) &&
    !route
  ) {
    return `#/projects/${encodeURIComponent(projectId)}?view=runs`;
  }
  const experimentId =
    typeof experimentSelection === "string"
      ? (route?.experiment_id ?? experimentSelection)
      : experimentSelection.experiment_id;
  const fields = [
    "view=runs",
    `experiment=${encodeURIComponent(experimentId)}`,
    ...(route
      ? [
          `episode=${encodeURIComponent(route.episode_id)}`,
          `target=${route.graph_target.kind}`,
          ...(route.graph_target.kind === "branch"
            ? [`branch=${encodeURIComponent(route.graph_target.branch_id)}`]
            : []),
          ...(route.parent_episode_id
            ? [`parent=${encodeURIComponent(route.parent_episode_id)}`]
            : []),
        ]
      : []),
  ];
  return `#/projects/${encodeURIComponent(projectId)}?${fields.join("&")}`;
}

function selectedExperimentHref(
  projectId: string,
  experimentId: string,
  exactRoute: ExperimentRouteIdentity | null,
): string {
  return experimentBoardHref(
    projectId,
    exactRoute?.experiment_id === experimentId ? exactRoute : experimentId,
  );
}

export function exactRunExperimentSelectionHref(
  projectId: string | null,
  experimentId: string | null,
  exactExperimentRoute: ExperimentRouteIdentity | null,
  exactAutoResearchEpisodeId: string | null,
  selectionKind: "select" | "show" = "select",
): string | null {
  if (!projectId || !experimentId || (!exactExperimentRoute && !exactAutoResearchEpisodeId)) {
    return null;
  }
  if (selectionKind === "show") return experimentBoardHref(projectId, experimentId);
  return selectedExperimentHref(projectId, experimentId, exactExperimentRoute);
}

// A started or reauthorized episode replaces an exact Auto-research selection. A Runs view that
// was not opened by exact episode keeps what it was showing, and an episode whose project is no
// longer the active tab routes nothing: its response must not address the project now on screen.
export function exactAutoResearchEpisodeHref(
  activeProjectId: string | null,
  episodeProjectId: string,
  episodeId: string,
  exactAutoResearchEpisodeId: string | null,
): string | null {
  if (!activeProjectId || activeProjectId !== episodeProjectId) return null;
  if (!episodeId || !exactAutoResearchEpisodeId) return null;
  return experimentBoardHref(episodeProjectId, `${AUTO_RESEARCH_ROUTE_PREFIX}${episodeId}`);
}

export function parseProjectHash(hash: string): ProjectHashRoute {
  const queryStart = hash.indexOf("?");
  const pathname = queryStart === -1 ? hash : hash.slice(0, queryStart);
  const match = pathname.match(/^#\/projects\/([^/]+)$/);
  if (!match || match[1] === "new") {
    return {
      projectId: null,
      view: "overview",
      projectViewSpecified: false,
      experimentId: null,
      experimentRoute: null,
      autoResearchEpisodeId: null,
    };
  }
  let projectId: string;
  try {
    projectId = decodeURIComponent(match[1]);
  } catch {
    return {
      projectId: null,
      view: "overview",
      projectViewSpecified: false,
      experimentId: null,
      experimentRoute: null,
      autoResearchEpisodeId: null,
    };
  }
  const params = new URLSearchParams(queryStart === -1 ? "" : hash.slice(queryStart + 1));
  if (params.get("view") !== "runs") {
    return {
      projectId,
      view: "overview",
      projectViewSpecified: params.has("view"),
      experimentId: null,
      experimentRoute: null,
      autoResearchEpisodeId: null,
    };
  }
  const encodedExperiment = params.get("experiment");
  if (!encodedExperiment) {
    const autoResearchEpisodeId = autoResearchEpisodeFromParams(params);
    return {
      projectId,
      view: "execution",
      projectViewSpecified: true,
      experimentId: null,
      experimentRoute: null,
      autoResearchEpisodeId,
    };
  }
  const experimentRoute = experimentRouteFromParams(encodedExperiment, params);
  if (hasExperimentIdentityParams(params) && !experimentRoute) {
    return {
      projectId,
      view: "execution",
      projectViewSpecified: true,
      experimentId: null,
      experimentRoute: null,
      autoResearchEpisodeId: null,
    };
  }
  return {
    projectId,
    view: "execution",
    projectViewSpecified: true,
    experimentId: encodedExperiment,
    experimentRoute,
    autoResearchEpisodeId: null,
  };
}

export function projectHashAfterViewChange(hash: string, nextView: AppView): string | null {
  const route = parseProjectHash(hash);
  if (nextView === "execution" || route.view !== "execution" || !route.projectId) return null;
  return `#/projects/${encodeURIComponent(route.projectId)}`;
}

export function experimentIndexEntryForRoute(
  entries: ExperimentLoopIndexEntry[],
  projectId: string | null,
  route: ExperimentRouteIdentity | null,
): ExperimentLoopIndexEntry | null {
  if (!projectId || !route) return null;
  const matches = entries.filter(
    (entry) =>
      entry.project_id === projectId &&
      entry.node.id === route.experiment_id &&
      entry.control.episode_id === route.episode_id &&
      entry.episode?.episode_id === route.episode_id &&
      entry.parent_episode_id === route.parent_episode_id &&
      graphTargetsEqual(entry.graph_target, route.graph_target) &&
      (!entry.graph_head || graphTargetsEqual(entry.graph_head.target, route.graph_target)),
  );
  return matches.length === 1 ? matches[0] : null;
}

export function projectExperimentExecution(
  nodes: GraphNode[],
  tasks: AgentTask[],
  watchers: WatcherRecord[],
  experimentControl: Record<string, ExperimentControlState>,
  route: ExperimentRouteIdentity | null,
  exactEntry: ExperimentLoopIndexEntry | null,
): ExperimentExecutionProjection {
  if (route?.graph_target.kind !== "branch") {
    return {
      nodes,
      tasks,
      watchers,
      experimentControl,
      exactBranchEntry: null,
      staleMainRoute:
        route && !mainExperimentRouteMatchesControl(route, experimentControl[route.experiment_id])
          ? route
          : null,
    };
  }

  const nodeId = route.experiment_id;
  const projectedNodes = nodes.filter(
    (node) => !(node.type === "experiment" && node.id === nodeId),
  );
  const projectedTasks = tasks.filter((task) => {
    if (task.request.patch_kind !== "experiment_loop" || task.request.control_node_id !== nodeId) {
      return true;
    }
    return (
      task.request.control_episode_id === route.episode_id &&
      graphTargetsEqual(task.graph_target, route.graph_target)
    );
  });
  const projectedWatchers = watchers.filter((watcher) => {
    if (
      watcher.continuation.patch_kind !== "experiment_loop" ||
      watcher.continuation.control_node_id !== nodeId
    ) {
      return true;
    }
    return (
      watcher.continuation.control_episode_id === route.episode_id &&
      graphTargetsEqual(watcher.graph_target, route.graph_target)
    );
  });
  const projectedControl = { ...experimentControl };
  delete projectedControl[nodeId];
  if (!exactEntry) {
    return {
      nodes: projectedNodes,
      tasks: projectedTasks,
      watchers: projectedWatchers,
      experimentControl: projectedControl,
      exactBranchEntry: null,
      staleMainRoute: null,
    };
  }

  const tasksById = new Map(projectedTasks.map((task) => [task.operation_id, task]));
  for (const task of exactEntry.episode?.tasks ?? []) tasksById.set(task.operation_id, task);
  projectedControl[nodeId] = exactEntry.control;
  return {
    nodes: [...projectedNodes, exactEntry.node],
    tasks: [...tasksById.values()],
    watchers: projectedWatchers,
    experimentControl: projectedControl,
    exactBranchEntry: exactEntry,
    staleMainRoute: null,
  };
}

export function mainExperimentRouteMatchesControl(
  route: ExperimentRouteIdentity,
  control: ExperimentControlState | undefined,
): boolean {
  return Boolean(
    route.graph_target.kind === "main" &&
    control?.episode_id === route.episode_id &&
    control.episode?.episode_id === route.episode_id &&
    graphTargetsEqual(control.episode.graph_target, route.graph_target),
  );
}

export function experimentStopPath(
  apiBase: string,
  experimentId: string,
  episodeId: string | null = null,
): string {
  const path = `${apiBase}/experiments/${encodeURIComponent(experimentId)}/stop`;
  return episodeId ? `${path}?episode_id=${encodeURIComponent(episodeId)}` : path;
}

export function branchExperimentPollingKey(
  projectId: string | null,
  route: ExperimentRouteIdentity | null,
): string | null {
  if (!projectId || route?.graph_target.kind !== "branch") return null;
  return JSON.stringify([
    projectId,
    route.experiment_id,
    route.episode_id,
    route.graph_target.branch_id,
    route.parent_episode_id,
  ]);
}

function experimentRouteIdentity(entry: ExperimentLoopIndexEntry): ExperimentRouteIdentity {
  const episodeId = entry.episode?.episode_id ?? entry.control.episode_id;
  if (!episodeId) throw new Error("An indexed Experiment route requires its durable episode.");
  return {
    experiment_id: entry.node.id,
    episode_id: episodeId,
    graph_target: entry.graph_target,
    parent_episode_id: entry.parent_episode_id,
  };
}

function experimentRouteFromToken(value: string): ExperimentRouteIdentity | null {
  if (!value.startsWith(INDEX_ROUTE_PREFIX)) return null;
  try {
    const candidate = JSON.parse(value.slice(INDEX_ROUTE_PREFIX.length)) as unknown;
    return parseExperimentRouteIdentity(candidate);
  } catch {
    return null;
  }
}

function experimentRouteFromParams(
  experimentId: string,
  params: URLSearchParams,
): ExperimentRouteIdentity | null {
  if (!hasExperimentIdentityParams(params)) return null;
  const episodeId = params.get("episode");
  const targetKind = params.get("target");
  const branchId = params.get("branch");
  const parentEpisodeId = params.get("parent");
  if (!episodeId) return null;
  if (targetKind === "main" && !branchId && !parentEpisodeId) {
    return {
      experiment_id: experimentId,
      episode_id: episodeId,
      graph_target: { kind: "main" },
      parent_episode_id: null,
    };
  }
  if (targetKind === "branch" && branchId && parentEpisodeId === branchId) {
    return {
      experiment_id: experimentId,
      episode_id: episodeId,
      graph_target: { kind: "branch", branch_id: branchId },
      parent_episode_id: parentEpisodeId,
    };
  }
  return null;
}

function autoResearchEpisodeFromParams(params: URLSearchParams): string | null {
  if (params.get("mode") !== "auto_research") return null;
  const episodeId = params.get("episode");
  if (
    !episodeId ||
    params.has("experiment") ||
    params.has("target") ||
    params.has("branch") ||
    params.has("parent")
  ) {
    return null;
  }
  return episodeId;
}

function parseExperimentRouteIdentity(candidate: unknown): ExperimentRouteIdentity | null {
  if (!candidate || typeof candidate !== "object") return null;
  const value = candidate as Record<string, unknown>;
  if (
    typeof value.experiment_id !== "string" ||
    !value.experiment_id ||
    typeof value.episode_id !== "string" ||
    !value.episode_id
  ) {
    return null;
  }
  const graphTarget = value.graph_target;
  if (!graphTarget || typeof graphTarget !== "object") return null;
  const target = graphTarget as Record<string, unknown>;
  if (target.kind === "main" && value.parent_episode_id === null) {
    return {
      experiment_id: value.experiment_id,
      episode_id: value.episode_id,
      graph_target: { kind: "main" },
      parent_episode_id: null,
    };
  }
  if (
    target.kind === "branch" &&
    typeof target.branch_id === "string" &&
    target.branch_id &&
    value.parent_episode_id === target.branch_id
  ) {
    return {
      experiment_id: value.experiment_id,
      episode_id: value.episode_id,
      graph_target: { kind: "branch", branch_id: target.branch_id },
      parent_episode_id: target.branch_id,
    };
  }
  return null;
}

function hasExperimentIdentityParams(params: URLSearchParams): boolean {
  return ["episode", "target", "branch", "parent"].some((key) => params.has(key));
}

export function graphTargetsEqual(left: GraphTargetRef, right: GraphTargetRef): boolean {
  return (
    left?.kind === right.kind &&
    (left.kind === "main" || (right.kind === "branch" && left.branch_id === right.branch_id))
  );
}
