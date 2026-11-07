import { createElement, type ReactNode } from "react";
import { RefreshRuntimeContext } from "../../src/core/useResource";
import { bindArtifactResourceStore } from "../../src/artifacts/artifactViewerModel";
import { ResourceStore } from "../../src/core/resourceStore";
import {
  RefreshScheduler,
  type WatermarkRequest,
  type WatermarkResponse,
} from "../../src/core/refreshScheduler";
export const store = new ResourceStore();
let activeProject: string | null = null;
/** Marks the fixture's shown project active and open, as the app does for its routed tab. */
export function activateProject(projectId: string | null): void {
  activeProject = projectId;
  store.setActiveProject(projectId);
}
/**
 * The fixture's watermark answer; `agentThreadsVersion` and `agentCardsVersion` move
 * the agent-thread and open-card watermarks.
 */
const answer = (request: WatermarkRequest): WatermarkResponse => ({
  agent_threads: request.agent_threads
    ? {
        version: String(
          (globalThis as { agentThreadsVersion?: string }).agentThreadsVersion ?? "1",
        ),
      }
    : null,
  agent_cards: request.agent_cards
    ? {
        version: String((globalThis as { agentCardsVersion?: string }).agentCardsVersion ?? "1"),
      }
    : null,
  epoch: "fixture",
  as_of: new Date().toISOString(),
  unavailable: [],
  projects: request.open.map((project) => ({
    requested_id: project.project_id,
    project_id: project.project_id,
    resources: project.resources.map((resource) => ({
      ...resource,
      version: "fixture",
      valid_until: null,
    })),
    sync: { last_completed_at: null, status: "local", error: null },
  })),
});
/** `?wire` also posts each beat over HTTP so a test can count it like the app's. */
const wired = typeof location !== "undefined" && new URLSearchParams(location.search).has("wire");
export const runtime = {
  store,
  scheduler: new RefreshScheduler({
    store,
    clientId: "fixture",
    openProjects: () => {
      const open = store.awaitingWatermark();
      if (activeProject) open.add(activeProject);
      return [...open].filter(Boolean).map((projectId) => ({ projectId, currentRef: "main" }));
    },
    activeProjectId: () => activeProject,
    onLostAccess: () => {},
    send: async (request, signal) => {
      if (wired)
        await fetch("/api/resource-watermarks", {
          method: "POST",
          body: JSON.stringify(request),
          signal,
        });
      return answer(request);
    },
  }),
};

runtime.scheduler.start();
bindArtifactResourceStore(store);

export function ResourceRuntime({ children }: { children: ReactNode }) {
  return createElement(RefreshRuntimeContext.Provider, { value: runtime }, children);
}
