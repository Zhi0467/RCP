import { createElement, type ReactNode } from "react";
import { RefreshRuntimeContext } from "../../src/core/useResource";
import { bindArtifactResourceStore } from "../../src/artifacts/artifactViewerModel";
import { ResourceStore } from "../../src/core/resourceStore";
import { RefreshScheduler } from "../../src/core/refreshScheduler";
export const store = new ResourceStore();
export const runtime = {
  store,
  scheduler: new RefreshScheduler({
    store,
    clientId: "fixture",
    openProjects: () =>
      [...store.awaitingWatermark()].map((projectId) => ({ projectId, currentRef: "main" })),
    activeProjectId: () => null,
    onLostAccess: () => {},
    send: async (request) => ({
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
    }),
  }),
};

runtime.scheduler.start();
bindArtifactResourceStore(store);

export function ResourceRuntime({ children }: { children: ReactNode }) {
  return createElement(RefreshRuntimeContext.Provider, { value: runtime }, children);
}
