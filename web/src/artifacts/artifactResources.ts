import { api } from "../core/api";
import type { ResourceProfile } from "../core/resourceStore";
import { projectResourceKey } from "../projects/projectResources";
import type { ArtifactViewerState, ProjectArtifact, RunArtifactEntry } from "../core/types";

export type InlineArtifactState = Pick<
  ArtifactViewerState,
  | "current_version"
  | "version_number"
  | "viewer_url"
  | "can_comment"
  | "can_keep"
  | "fresh_session_required"
  | "live"
  | "editing_operation_id"
>;

export function spaceArtifactStateProfile(
  threadId: string,
  artifactId: string,
): ResourceProfile<InlineArtifactState> {
  return {
    key: {
      backend: "",
      member: "",
      project: "",
      name: "agent_threads",
      entity: threadId,
      query: `artifact:${artifactId}`,
    },
    fetch: (signal) =>
      api(
        `/api/agent-threads/${encodeURIComponent(threadId)}/artifacts/${encodeURIComponent(artifactId)}/state`,
        { signal },
      ),
  };
}

export function artifactsProfile(projectId: string): ResourceProfile<ProjectArtifact[]> {
  return {
    key: projectResourceKey(projectId, "artifacts"),
    fetch: (signal) => api(`/api/projects/${encodeURIComponent(projectId)}/artifacts`, { signal }),
  };
}
export function artifactStateProfile(
  projectId: string,
  artifactId: string,
): ResourceProfile<ArtifactViewerState> {
  return {
    key: projectResourceKey(projectId, "artifacts", { query: `state:${artifactId}` }),
    fetch: (signal) =>
      api(
        `/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/state`,
        { signal },
      ),
  };
}
export function runArtifactsProfile(
  projectId: string,
  episodeId: string,
): ResourceProfile<RunArtifactEntry[]> {
  return {
    watch: [{ name: "chats" }],
    key: projectResourceKey(projectId, "artifacts", { query: `episode:${episodeId}` }),
    fetch: (signal) =>
      api(
        `/api/projects/${encodeURIComponent(projectId)}/episodes/${encodeURIComponent(episodeId)}/artifacts`,
        { signal },
      ),
  };
}
