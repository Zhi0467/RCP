import type { ResourceProfile } from "../core/resourceStore";
import type { ArtifactViewerState } from "../core/types";
import { api, steerChatTurn } from "../core/api";
import { artifactUrl } from "../agents/agentTasks";
import { controlThreadTask, loadThreadTask, steerThreadTurn } from "../agents/agentThreadsApi";
import { artifactStateProfile } from "../artifacts/artifactResources";
import { openArtifact, openSpaceArtifact } from "../artifacts/artifactViewerModel";
import {
  downloadDesktopArtifact,
  isDesktopRuntime,
  openDesktopArtifactPdf,
} from "../core/desktopRuntime";
import type {
  AgentArtifactDescriptor,
  AgentTask,
  AgentThread,
  ChatMessage,
  SteerRequest,
} from "../core/types";

type ArtifactAction = Parameters<typeof artifactUrl>[3];

/** Route and capability boundary for a project chat or a member's Space window. */
export interface AgentWindowEndpoints {
  scopeKey: string;
  projectId: string | null;
  attachmentsUrl: string;
  artifactUrl: (operationId: string, artifactId: string, action: ArtifactAction) => string;
  artifactState: ((artifactId: string) => ResourceProfile<ArtifactViewerState>) | null;
  canKeep: boolean;
  canCommentArtifacts: boolean;
  openArtifactPanel: ((artifactId: string) => void) | null;
  openArtifact: (operationId: string, artifact: AgentArtifactDescriptor) => Promise<void>;
  downloadArtifact:
    ((operationId: string, artifact: AgentArtifactDescriptor) => Promise<unknown>) | null;
  loadTask: (operationId: string) => Promise<AgentTask>;
  control: (
    operationId: string,
    action: "pause" | "resume" | "retry",
    requestId: string,
  ) => Promise<AgentTask>;
  steer: (operationId: string, request: SteerRequest) => Promise<ChatMessage>;
  questionsApiBase: string | null;
  /** The member project a Space reply's path or saved artifact id belongs to; a
   *  project window resolves its own references, so it has none. */
  referenceProject: ((query: { path: string } | { artifact_id: string }) => Promise<string>) | null;
}

export function projectAgentWindowEndpoints(
  projectId: string,
  chatId: string,
): AgentWindowEndpoints {
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  const owner = { kind: "project" as const, project_id: projectId, thread_id: chatId };
  return {
    scopeKey: `${projectId}:${chatId}`,
    projectId,
    attachmentsUrl: `${base}/chats/${encodeURIComponent(chatId)}/attachments`,
    artifactUrl: (operation, artifact, action) =>
      artifactUrl(projectId, operation, artifact, action),
    artifactState: (artifactId) => artifactStateProfile(projectId, artifactId),
    canKeep: true,
    canCommentArtifacts: true,
    openArtifactPanel: (artifactId) => openArtifact({ projectId, artifactId }),
    openArtifact: async (taskId, artifact) => {
      if (artifact.view !== "pdf") openArtifact({ projectId, artifactId: artifact.artifact_id });
      else if (isDesktopRuntime())
        await openDesktopArtifactPdf({ projectId, taskId, artifactId: artifact.artifact_id });
    },
    downloadArtifact: isDesktopRuntime()
      ? (taskId, artifact) =>
          downloadDesktopArtifact({
            projectId,
            taskId,
            artifactId: artifact.artifact_id,
            suggestedName: artifact.name,
          })
      : null,
    loadTask: (operationId) => loadThreadTask(owner, operationId),
    control: (operationId, action, requestId) =>
      controlThreadTask(owner, operationId, action, requestId),
    steer: (operationId, request) => steerChatTurn(projectId, operationId, request),
    questionsApiBase: base,
    referenceProject: null,
  };
}

export function spaceAgentWindowEndpoints(thread: AgentThread): AgentWindowEndpoints {
  const base = `/api/agent-threads/${encodeURIComponent(thread.thread_id)}`;
  const url: AgentWindowEndpoints["artifactUrl"] = (operation, artifact, action) =>
    `${base}/tasks/${encodeURIComponent(operation)}/artifacts/${encodeURIComponent(artifact)}/${action}`;
  return {
    scopeKey: `space:${thread.thread_id}`,
    projectId: null,
    attachmentsUrl: `${base}/attachments`,
    artifactUrl: url,
    artifactState: null,
    canKeep: false,
    canCommentArtifacts: false,
    openArtifactPanel: null,
    openArtifact: async (operation, artifact) => {
      openSpaceArtifact({
        viewerUrl: url(operation, artifact.artifact_id, "viewer"),
        name: artifact.name,
      });
    },
    downloadArtifact: null,
    loadTask: (operation) => loadThreadTask(thread, operation),
    control: (operation, action, requestId) =>
      controlThreadTask(thread, operation, action, requestId),
    steer: (operation, request) => steerThreadTurn(thread, operation, request),
    questionsApiBase: null,
    referenceProject: (query) =>
      api<{ project_id: string }>(`${base}/references?${new URLSearchParams(query)}`).then(
        (found) => found.project_id,
      ),
  };
}
