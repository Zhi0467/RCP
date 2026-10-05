import { graphTargetUrl, MAIN_GRAPH } from "./graphTarget";
import type { AgentTasksSnapshot } from "./hooks/useAgentTasks";
import type { ChatStateSnapshot } from "./hooks/useChatState";
import type { GraphSelectionTabSnapshot } from "./hooks/useGraphSelection";
import type { ProjectHistorySnapshot } from "./hooks/useProjectHistory";
import {
  reconcileInactiveProjectSession,
  type ProjectSessionTabState,
} from "./hooks/projectSession";
import { applyHumanDraft } from "./humanDraft";
import { projectAttentionForPresentation } from "./projectAttention";
import type {
  AgentTask,
  AgentUsageSnapshot,
  ExperimentControlState,
  GraphAttentionProjection,
  GraphNode,
  GraphRevisionSnapshot,
  GraphState,
  GraphTargetRef,
  ProjectCard,
  ProjectSnapshot,
  WatcherRecord,
} from "./types";

export async function loadGraphRevision(
  fetchJson: <T>(path: string) => Promise<T>,
  apiBase: string,
): Promise<GraphRevisionSnapshot> {
  return fetchJson<GraphRevisionSnapshot>(`${apiBase}/cached/revision`);
}

/**
 * Open a project in two steps: the cached display snapshot paints first when the
 * session accepts it, and the authoritative reload always follows and finishes
 * the open. A cached snapshot the session declines — a newer snapshot request
 * already started, such as the active-tab heartbeat's reload, or a stale
 * revision — must never end the open early, or the view keeps its spinner with
 * nothing left to clear it.
 */
export async function openProjectSequence(steps: {
  applyCachedSnapshot: () => Promise<void>;
  reloadAuthoritative: () => Promise<void>;
  stillOpening: () => boolean;
  settle: () => void;
}): Promise<void> {
  await steps.applyCachedSnapshot();
  if (!steps.stillOpening()) return;
  try {
    await steps.reloadAuthoritative();
  } finally {
    if (steps.stillOpening()) steps.settle();
  }
}

export function canonicalRevisionNeedsReload(
  observedRevision: number,
  renderedRevision: number,
  observedMutation?: ProjectSnapshot["graph_mutation"],
  renderedMutation?: ProjectSnapshot["graph_mutation"],
): boolean {
  return (
    observedRevision > renderedRevision ||
    Boolean(
      observedMutation &&
      renderedMutation &&
      (observedMutation.available !== renderedMutation.available ||
        observedMutation.reason !== renderedMutation.reason),
    )
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

export async function loadExperimentWatcherPoll(
  fetchJson: <T>(path: string) => Promise<T>,
  base: string,
  graphTarget: GraphTargetRef = MAIN_GRAPH,
): Promise<{
  watchers: WatcherRecord[];
  tasks: AgentTask[];
  project: ProjectSnapshot;
}> {
  const [watchers, tasks, project] = await Promise.all([
    fetchJson<WatcherRecord[]>(graphTargetUrl(`${base}/watchers`, graphTarget)),
    fetchJson<AgentTask[]>(`${base}/tasks`),
    fetchJson<ProjectSnapshot>(graphTargetUrl(base, graphTarget)),
  ]);
  return { watchers, tasks, project };
}

export interface CachedProjectTabState
  extends
    ProjectHistorySnapshot,
    AgentTasksSnapshot,
    ChatStateSnapshot,
    GraphSelectionTabSnapshot,
    ProjectSessionTabState {
  project: ProjectSnapshot;
  projectHeaderCollapsed: boolean;
  usage: AgentUsageSnapshot | null;
  watchers: WatcherRecord[];
}

export function reconcileInactiveProjectTabState(
  state: CachedProjectTabState,
  snapshot: ProjectSnapshot,
): CachedProjectTabState {
  const session = reconcileInactiveProjectSession(state, snapshot);
  if (session === state) return state;
  if (!session.project) return state;
  const presented = applyHumanDraft(session.project.graph, session.humanDraft);
  return {
    ...state,
    ...session,
    project: session.project,
    selectedNodeId:
      state.selectedNodeId && presented.nodes[state.selectedNodeId] ? state.selectedNodeId : null,
    companionNodeId:
      state.companionNodeId && presented.nodes[state.companionNodeId]
        ? state.companionNodeId
        : null,
    floatingChat:
      state.floatingChat && presented.nodes[state.floatingChat.nodeId] ? state.floatingChat : null,
  };
}

export function projectWithGraph(
  project: ProjectSnapshot,
  graph: GraphState,
  attention: GraphAttentionProjection = projectAttentionForPresentation(project, null),
  primaryQuestion: GraphNode | null = project.primary_question ?? null,
  counts = project.counts,
): ProjectSnapshot {
  return {
    ...project,
    graph,
    revision: graph.revision,
    primary_question: primaryQuestion,
    attention,
    counts,
  };
}

export function projectWithTransitionProjection(
  project: ProjectSnapshot,
  graph: GraphState,
  experimentControl: Record<string, ExperimentControlState>,
  attention: GraphAttentionProjection,
  primaryQuestion: GraphNode | null,
  counts: ProjectSnapshot["counts"],
): ProjectSnapshot {
  return {
    ...projectWithGraph(project, graph, attention, primaryQuestion, counts),
    experiment_control: experimentControl,
  };
}

export function emptyProjectCounts(): ProjectSnapshot["counts"] {
  return {
    pending_proposals: 0,
    decisions_awaiting_choice: 0,
    open_blockers: 0,
    asserted: 0,
    accepted: 0,
    contested: 0,
  };
}
