import { useSyncExternalStore } from "react";
import { api } from "./api";
import type { RunArtifactEntry } from "./types";

export type ArtifactViewerTarget =
  | { kind: "artifact"; projectId: string; artifactId: string }
  | { kind: "repository"; projectId: string; path: string; line?: number | null };
let target: ArtifactViewerTarget | null = null;
const listeners = new Set<() => void>();
function publish(next: ArtifactViewerTarget | null) {
  target = next;
  listeners.forEach((listener) => listener());
}
export function openArtifact(input: { projectId: string; artifactId: string }): void {
  publish({ kind: "artifact", ...input });
}
export function openRepositoryFile(input: {
  projectId: string;
  path: string;
  line?: number | null;
}): void {
  publish({ kind: "repository", ...input });
}
export function closeArtifactViewer(): void {
  publish(null);
}
export function useArtifactViewerTarget() {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    () => target,
  );
}

export async function openEpisodeReport(input: {
  projectId: string;
  episodeId: string;
}): Promise<void> {
  const artifacts = await api<RunArtifactEntry[]>(
    `/api/projects/${encodeURIComponent(input.projectId)}/episodes/${encodeURIComponent(input.episodeId)}/artifacts`,
  );
  const report = artifacts.find((artifact) => artifact.supplier === "episode_ending");
  if (!report) throw new Error("The episode report is unavailable.");
  openArtifact({ projectId: input.projectId, artifactId: report.artifact_id });
}
