import {
  buildExperimentRun,
  experimentRunSection,
  type ExperimentLoopHealth,
} from "./runProjection";
import type { AgentTaskStatus, AppView, ExperimentLoopIndexEntry } from "./types";

export type ExperimentBoardSection = "needs_action" | "in_progress" | "finished";

export interface ExperimentBoardItem {
  entry: ExperimentLoopIndexEntry;
  health: ExperimentLoopHealth;
  section: ExperimentBoardSection;
  lastActivityAt: string | null;
}

export interface ExperimentBoardProjection {
  needsAction: ExperimentBoardItem[];
  inProgress: ExperimentBoardItem[];
  finished: ExperimentBoardItem[];
}

export interface ProjectHashRoute {
  projectId: string | null;
  view: AppView;
  experimentId: string | null;
}

export function buildExperimentBoard(
  entries: ExperimentLoopIndexEntry[],
): ExperimentBoardProjection {
  const projection: ExperimentBoardProjection = {
    needsAction: [],
    inProgress: [],
    finished: [],
  };
  for (const entry of entries) {
    const run = buildExperimentRun(entry.node, entry.control, [], []);
    const runSection = experimentRunSection(
      run.health,
      entry.control.operational.current_status as AgentTaskStatus | null,
    );
    const item: ExperimentBoardItem = {
      entry,
      health: run.health,
      section:
        runSection === "running"
          ? "in_progress"
          : runSection === "actionable"
            ? "needs_action"
            : "finished",
      lastActivityAt: entry.control.operational.current_last_activity_at,
    };
    if (item.section === "needs_action") projection.needsAction.push(item);
    else if (item.section === "in_progress") projection.inProgress.push(item);
    else projection.finished.push(item);
  }
  projection.needsAction.sort(compareBoardItems);
  projection.inProgress.sort(compareBoardItems);
  projection.finished.sort(compareBoardItems);
  return projection;
}

export function experimentTerminalLabel(status: unknown): string {
  if (status === "completed") return "Succeeded";
  if (status === "abandoned") return "Abandoned";
  if (status === "superseded") return "Superseded";
  return "Completed";
}

export function experimentBoardHref(projectId: string, experimentId: string): string {
  return `#/projects/${encodeURIComponent(projectId)}?view=runs&experiment=${encodeURIComponent(experimentId)}`;
}

export function parseProjectHash(hash: string): ProjectHashRoute {
  const queryStart = hash.indexOf("?");
  const pathname = queryStart === -1 ? hash : hash.slice(0, queryStart);
  const match = pathname.match(/^#\/projects\/([^/]+)$/);
  if (!match || match[1] === "new") {
    return { projectId: null, view: "overview", experimentId: null };
  }
  let projectId: string;
  try {
    projectId = decodeURIComponent(match[1]);
  } catch {
    return { projectId: null, view: "overview", experimentId: null };
  }
  const params = new URLSearchParams(queryStart === -1 ? "" : hash.slice(queryStart + 1));
  if (params.get("view") !== "runs") {
    return { projectId, view: "overview", experimentId: null };
  }
  const encodedExperiment = params.get("experiment");
  if (!encodedExperiment) return { projectId, view: "execution", experimentId: null };
  return { projectId, view: "execution", experimentId: encodedExperiment };
}

export function projectHashAfterViewChange(hash: string, nextView: AppView): string | null {
  const route = parseProjectHash(hash);
  if (nextView === "execution" || route.view !== "execution" || !route.projectId) return null;
  return `#/projects/${encodeURIComponent(route.projectId)}`;
}

function compareBoardItems(left: ExperimentBoardItem, right: ExperimentBoardItem): number {
  const leftActivity = left.lastActivityAt ?? "";
  const rightActivity = right.lastActivityAt ?? "";
  return (
    rightActivity.localeCompare(leftActivity) ||
    right.entry.node.updated_rev - left.entry.node.updated_rev ||
    left.entry.project_name.localeCompare(right.entry.project_name) ||
    left.entry.project_id.localeCompare(right.entry.project_id) ||
    left.entry.node.id.localeCompare(right.entry.node.id)
  );
}
