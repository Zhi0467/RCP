import type { ChatUiState } from "./chat/useChatState";
import type { GraphSelectionTabSnapshot } from "./graph/useGraphSelection";
import type { ProjectHistoryUiState } from "./graph/useProjectHistory";
import { type ProjectSessionTabState } from "./graph/projectSession";
import type { PaperEditorState } from "./paper/PaperWorkspace";
import { projectAttentionForPresentation } from "./projectAttention";
import type {
  ExperimentControlState,
  GraphAttentionProjection,
  GraphNode,
  GraphState,
  ProjectSnapshot,
} from "./core/types";

export interface ProjectTabUiState
  extends ProjectHistoryUiState, ChatUiState, GraphSelectionTabSnapshot, ProjectSessionTabState {
  projectHeaderCollapsed: boolean;
  taskInspectorId: string | null;
  activityTaskId: string | null;
  retryTaskId: string | null;
  paperEditorState?: PaperEditorState;
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
