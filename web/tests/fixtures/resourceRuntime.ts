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
