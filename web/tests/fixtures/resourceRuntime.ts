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
    openProjects: () => [],
    activeProjectId: () => null,
    onLostAccess: () => {},
    send: async () => ({ epoch: "fixture", as_of: "", projects: [], unavailable: [] }),
  }),
};

bindArtifactResourceStore(store);

export function ResourceRuntime({ children }: { children: ReactNode }) {
  return createElement(RefreshRuntimeContext.Provider, { value: runtime }, children);
}
