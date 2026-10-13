import { useSyncExternalStore } from "react";
import type { ResourceStore } from "../core/resourceStore";
import { runArtifactsProfile } from "./artifactResources";
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
  return useSyncExternalStore(subscribeShell, currentDockedArtifact, () => null);
}
export function setArtifactViewerHidden(next: boolean): void {
  if (hidden === next) return;
  hidden = next;
  shellListeners.forEach((listener) => listener());
}
export function useArtifactViewerHidden(): boolean {
  return useSyncExternalStore(
    subscribeShell,
    () => hidden,
    () => false,
  );
}

let resourceStore: ResourceStore | null = null;

/** The App runtime owns this binding, including backend/member replacement. */
export function bindArtifactResourceStore(store: ResourceStore): () => void {
  resourceStore = store;
  return () => {
    if (resourceStore === store) resourceStore = null;
  };
}

export async function openEpisodeReport(input: {
  projectId: string;
  episodeId: string;
}): Promise<void> {
  const generation = ++requestGeneration;
  const store = resourceStore;
  if (!store) throw new Error("Artifact refresh runtime is unavailable.");
  const id = store.register(input.projectId, runArtifactsProfile(input.projectId, input.episodeId));
  const artifacts = await new Promise<RunArtifactEntry[]>((resolve, reject) => {
    const read = () => {
      const view = store.view<RunArtifactEntry[]>(id);
      if (view.value !== undefined) {
        unsubscribe();
        resolve(view.value);
      } else if (
        view.error ||
        resourceStore !== store ||
        store.requests(input.projectId).length === 0
      ) {
        unsubscribe();
        reject(view.error ?? new Error("Artifact refresh target was closed."));
      }
    };
    const unsubscribe = store.subscribe(read);
    read();
  });
  if (generation !== requestGeneration || resourceStore !== store) return;
  const report = artifacts.find((artifact) => artifact.supplier === "episode_ending");
  if (!report) throw new Error("The episode report is unavailable.");
  openArtifact({ projectId: input.projectId, artifactId: report.artifact_id });
}
