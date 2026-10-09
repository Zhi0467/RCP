import { useSyncExternalStore } from "react";
import { api } from "../core/api";
import type { RunArtifactEntry } from "../core/types";

export type ArtifactViewerTarget =
  | { kind: "artifact"; projectId: string; artifactId: string }
  | { kind: "repository"; projectId: string; path: string; line?: number | null };
let target: ArtifactViewerTarget | null = null;
let requestGeneration = 0;
const listeners = new Set<() => void>();
function publish(next: ArtifactViewerTarget | null) {
  requestGeneration += 1;
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

// The panel learns of version moves an inline copy of the same artifact cannot
// see, such as Undo, which settles no task.
const versionListeners = new Set<(artifactId: string) => void>();
export function announceArtifactVersionChange(artifactId: string): void {
  versionListeners.forEach((listener) => listener(artifactId));
}
export function onArtifactVersionChange(listener: (artifactId: string) => void): () => void {
  versionListeners.add(listener);
  return () => {
    versionListeners.delete(listener);
  };
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

// A collapsed viewer lives in the project's dock, which the workspace renders;
// the workspace hides the viewer while a space-level page covers the project.
export interface DockedArtifact {
  title: string;
  restore: () => void;
}
let docked: DockedArtifact | null = null;
let hidden = false;
const shellListeners = new Set<() => void>();
function subscribeShell(listener: () => void) {
  shellListeners.add(listener);
  return () => {
    shellListeners.delete(listener);
  };
}
export function publishDockedArtifact(next: DockedArtifact | null): void {
  if (docked?.title === next?.title && docked?.restore === next?.restore) return;
  docked = next;
  shellListeners.forEach((listener) => listener());
}
export function currentDockedArtifact(): DockedArtifact | null {
  return docked;
}
export function useDockedArtifact(): DockedArtifact | null {
  return useSyncExternalStore(subscribeShell, currentDockedArtifact);
}
export function setArtifactViewerHidden(next: boolean): void {
  if (hidden === next) return;
  hidden = next;
  shellListeners.forEach((listener) => listener());
}
export function useArtifactViewerHidden(): boolean {
  return useSyncExternalStore(subscribeShell, () => hidden);
}

export async function openEpisodeReport(input: {
  projectId: string;
  episodeId: string;
}): Promise<void> {
  const generation = ++requestGeneration;
  const artifacts = await api<RunArtifactEntry[]>(
    `/api/projects/${encodeURIComponent(input.projectId)}/episodes/${encodeURIComponent(input.episodeId)}/artifacts`,
  );
  if (generation !== requestGeneration) return;
  const report = artifacts.find((artifact) => artifact.supplier === "episode_ending");
  if (!report) throw new Error("The episode report is unavailable.");
  openArtifact({ projectId: input.projectId, artifactId: report.artifact_id });
}
