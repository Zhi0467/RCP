import { artifactUrl, taskArtifacts } from "../agentTasks";
import { episodeReportPreviewUrl } from "../campaigns";
import type { AgentTask, ArtifactView, Episode, ProjectSnapshot } from "../types";
import {
  NEVER_CONFIRM,
  type WebMcpToolDefinition,
  type WebMcpToolSpec,
  compactText,
  optionalStringInput,
  requiredStringInput,
  webMcpTextResult,
  withExecute,
} from "./shared";

const WEBMCP_ARTIFACT_RESULT_MAX_CHARS = 8_000;
const ARTIFACT_LIST_LIMIT = 8;

type ArtifactFilter = {
  nodeId: string | null;
  chatId: string | null;
  taskId: string | null;
  episodeId: string | null;
};

export type ProjectArtifactRecord = {
  artifact_id: string | null;
  viewer_id: string;
  kind: "task_artifact" | "episode_report";
  name: string;
  media_type: string;
  view: ArtifactView;
  can_download: boolean;
  available: boolean;
  can_open: boolean;
  task_id: string | null;
  chat_id: string | null;
  node_id: string | null;
  episode_id: string | null;
  kept_filename: string | null;
  unavailable_reason: string | null;
  viewer_url: string;
  content_url: string;
  sort_time: string;
};

function artifactFilter(input: Record<string, unknown>): ArtifactFilter {
  const filter = {
    nodeId: optionalStringInput(input, "node_id"),
    chatId: optionalStringInput(input, "chat_id"),
    taskId: optionalStringInput(input, "task_id"),
    episodeId: optionalStringInput(input, "episode_id"),
  };
  if (Object.values(filter).filter(Boolean).length > 1) {
    throw new Error("Supply at most one artifact filter.");
  }
  return filter;
}

function taskNodeId(task: AgentTask): string | null {
  const value = task.request.control_node_id ?? task.request.node_id;
  return typeof value === "string" && value ? value : null;
}

export function taskChatId(task: AgentTask): string | null {
  const value = task.request.chat_id;
  return typeof value === "string" && value ? value : null;
}

function taskMatchesArtifactFilter(task: AgentTask, filter: ArtifactFilter): boolean {
  if (filter.nodeId) return taskNodeId(task) === filter.nodeId;
  if (filter.chatId) return taskChatId(task) === filter.chatId;
  if (filter.taskId) return task.operation_id === filter.taskId;
  if (filter.episodeId) return task.episode_id === filter.episodeId;
  return true;
}

function episodeMatchesArtifactFilter(
  episode: Episode,
  tasks: AgentTask[],
  filter: ArtifactFilter,
): boolean {
  if (filter.nodeId) return episode.control_node_id === filter.nodeId;
  if (filter.chatId) {
    return tasks.some(
      (task) => task.episode_id === episode.episode_id && taskChatId(task) === filter.chatId,
    );
  }
  if (filter.taskId) return false;
  if (filter.episodeId) return episode.episode_id === filter.episodeId;
  return true;
}

function assertArtifactFilterTarget(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  episodes: Episode[],
  filter: ArtifactFilter,
): void {
  if (filter.nodeId && !project.graph.nodes[filter.nodeId]) {
    throw new Error(`Node ${filter.nodeId} is not present in the current project graph.`);
  }
  if (filter.taskId && !tasks.some((task) => task.operation_id === filter.taskId)) {
    throw new Error(`Task ${filter.taskId} is not present in the current project.`);
  }
  if (filter.chatId && !tasks.some((task) => taskChatId(task) === filter.chatId)) {
    throw new Error(`Conversation ${filter.chatId} is not present in the current project.`);
  }
  if (filter.episodeId && !episodes.some((episode) => episode.episode_id === filter.episodeId)) {
    throw new Error(`Episode ${filter.episodeId} is not present in the current project.`);
  }
}

/** Resolve one exact episode id through the backend when it lies outside the
 * bounded recent-episode window the page holds; null means the backend has no
 * such episode. */
export type WebMcpArtifactSource = {
  loadEpisode: (episodeId: string) => Promise<Episode | null>;
  loadTask: (operationId: string) => Promise<AgentTask | null>;
};

export async function withExactEpisode(
  episodes: Episode[],
  episodeId: string | null,
  source: WebMcpArtifactSource,
): Promise<Episode[]> {
  if (!episodeId || episodes.some((episode) => episode.episode_id === episodeId)) return episodes;
  const exact = await source.loadEpisode(episodeId);
  return exact && exact.episode_id === episodeId ? [...episodes, exact] : episodes;
}

/** The page holds only the newest bounded task rows; an exact task id outside
 * that window is read through the existing task route so its saved artifacts
 * stay listable and openable. */
async function withExactTask(
  tasks: AgentTask[],
  operationId: string | null,
  source: WebMcpArtifactSource,
): Promise<AgentTask[]> {
  if (!operationId || tasks.some((task) => task.operation_id === operationId)) return tasks;
  const exact = await source.loadTask(operationId);
  return exact && exact.operation_id === operationId ? [...tasks, exact] : tasks;
}

function projectArtifactRecords(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  episodes: Episode[],
  filter: ArtifactFilter,
): ProjectArtifactRecord[] {
  assertArtifactFilterTarget(project, tasks, episodes, filter);
  const artifacts = tasks
    .filter((task) => taskMatchesArtifactFilter(task, filter))
    .flatMap((task) =>
      taskArtifacts(task).map((artifact) => ({
        artifact_id: artifact.artifact_id,
        viewer_id: `task:${task.operation_id}:${artifact.artifact_id}`,
        kind: "task_artifact" as const,
        name: compactText(artifact.name, 120),
        media_type: artifact.media_type,
        view: artifact.view,
        can_download: artifact.can_download,
        available: artifact.available,
        can_open: artifact.can_open,
        task_id: task.operation_id,
        chat_id: taskChatId(task),
        node_id: taskNodeId(task),
        episode_id: task.episode_id ?? null,
        kept_filename: artifact.kept_filename ?? null,
        unavailable_reason: artifact.unavailable_reason
          ? compactText(artifact.unavailable_reason, 160)
          : null,
        viewer_url: artifactUrl(project.id, task.operation_id, artifact.artifact_id, "viewer"),
        content_url: artifactUrl(project.id, task.operation_id, artifact.artifact_id, "content"),
        sort_time: task.updated_at,
      })),
    );
  const reports = episodes
    .filter(
      (episode) =>
        episode.report &&
        episode.wrapup_state === "ready" &&
        episodeMatchesArtifactFilter(episode, tasks, filter),
    )
    .map((episode) => ({
      artifact_id: null,
      viewer_id: `report:${episode.episode_id}`,
      kind: "episode_report" as const,
      name: `${episode.ending ?? "Experiment"} episode report`,
      media_type: "text/html",
      view: "html" as const,
      can_download: false,
      available: true,
      can_open: true,
      task_id: null,
      chat_id: null,
      node_id: episode.control_node_id,
      episode_id: episode.episode_id,
      kept_filename: null,
      unavailable_reason: null,
      viewer_url: episodeReportPreviewUrl(project.id, episode.episode_id),
      content_url: `/api/projects/${encodeURIComponent(project.id)}/episodes/${encodeURIComponent(episode.episode_id)}/report/content`,
      sort_time: episode.report?.created_at ?? episode.updated_at,
    }));
  return [...artifacts, ...reports].sort(
    (left, right) =>
      Date.parse(right.sort_time) - Date.parse(left.sort_time) ||
      left.viewer_id.localeCompare(right.viewer_id),
  );
}

export async function listProjectArtifacts(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  episodes: Episode[],
  input: Record<string, unknown>,
  source: WebMcpArtifactSource,
): Promise<Record<string, unknown>> {
  const filter = artifactFilter(input);
  const knownTasks = await withExactTask(tasks, filter.taskId, source);
  const knownEpisodes = await withExactEpisode(episodes, filter.episodeId, source);
  const records = projectArtifactRecords(project, knownTasks, knownEpisodes, filter);
  return {
    project_id: project.id,
    total: records.length,
    truncated: records.length > ARTIFACT_LIST_LIMIT,
    recent_task_count: tasks.length,
    recent_episode_count: episodes.length,
    artifacts: records
      .slice(0, ARTIFACT_LIST_LIMIT)
      .map(
        ({ sort_time: _, viewer_url: __, content_url: ___, artifact_id: ____, ...record }) =>
          record,
      ),
  };
}

const REPORT_VIEWER_PREFIX = "report:";
const TASK_VIEWER_PREFIX = "task:";

function taskViewerOperationId(viewerId: string): string | null {
  if (!viewerId.startsWith(TASK_VIEWER_PREFIX)) return null;
  const operationId = viewerId.slice(TASK_VIEWER_PREFIX.length).split(":")[0];
  return operationId || null;
}

export async function openProjectArtifact(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  episodes: Episode[],
  input: Record<string, unknown>,
  openViewer: (record: ProjectArtifactRecord, projectId: string) => boolean | Promise<boolean>,
  source: WebMcpArtifactSource,
): Promise<Record<string, unknown>> {
  const viewerId = requiredStringInput(input, "viewer_id");
  const reportEpisodeId = viewerId.startsWith(REPORT_VIEWER_PREFIX)
    ? viewerId.slice(REPORT_VIEWER_PREFIX.length)
    : null;
  const knownTasks = await withExactTask(tasks, taskViewerOperationId(viewerId), source);
  const knownEpisodes = await withExactEpisode(episodes, reportEpisodeId, source);
  const record = projectArtifactRecords(
    project,
    knownTasks,
    knownEpisodes,
    artifactFilter({}),
  ).find((candidate) => candidate.viewer_id === viewerId);
  if (!record)
    throw new Error(`Artifact viewer ${viewerId} is not present in the current project.`);
  if (!record.available || !record.can_open) {
    throw new Error(record.unavailable_reason ?? `Artifact viewer ${viewerId} is unavailable.`);
  }
  if (!(await openViewer(record, project.id))) {
    throw new Error("The RCP artifact viewer could not be shown.");
  }
  return {
    project_id: project.id,
    viewer_id: record.viewer_id,
    kind: record.kind,
    opened: true,
  };
}

export const LIST_ARTIFACTS_TOOL: WebMcpToolSpec = {
  name: "rcp_list_artifacts",
  description:
    "List RCP task artifacts and immutable episode reports in the open project. Results come from the recent task and episode windows unless an exact task_id or episode_id is supplied.",
  inputSchema: {
    type: "object",
    properties: {
      node_id: {
        type: "string",
        minLength: 1,
        description: "Optional exact graph node id whose artifacts should be listed.",
      },
      chat_id: {
        type: "string",
        minLength: 1,
        description: "Optional exact conversation id whose artifacts should be listed.",
      },
      task_id: {
        type: "string",
        minLength: 1,
        description: "Optional exact task id whose artifacts should be listed.",
      },
      episode_id: {
        type: "string",
        minLength: 1,
        description: "Optional exact episode id whose artifacts and report should be listed.",
      },
    },
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

export const OPEN_ARTIFACT_TOOL: WebMcpToolSpec = {
  name: "rcp_open_artifact",
  description: "Open one listed artifact or report in RCP's existing visual viewer.",
  inputSchema: {
    type: "object",
    properties: {
      viewer_id: {
        type: "string",
        minLength: 1,
        description: "Exact viewer id returned by rcp_list_artifacts.",
      },
    },
    required: ["viewer_id"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

export function projectArtifactToolDefinitions(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  episodes: Episode[],
  openViewer: (record: ProjectArtifactRecord, projectId: string) => boolean | Promise<boolean>,
  source: WebMcpArtifactSource,
): WebMcpToolDefinition[] {
  return [
    withExecute(LIST_ARTIFACTS_TOOL, async (input) =>
      webMcpTextResult(
        await listProjectArtifacts(project, tasks, episodes, input, source),
        WEBMCP_ARTIFACT_RESULT_MAX_CHARS,
      ),
    ),
    withExecute(OPEN_ARTIFACT_TOOL, async (input) =>
      webMcpTextResult(
        await openProjectArtifact(project, tasks, episodes, input, openViewer, source),
      ),
    ),
  ];
}
