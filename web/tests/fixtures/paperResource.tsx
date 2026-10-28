import React from "react";
import { createRoot } from "react-dom/client";
import { PaperWorkspace } from "../../src/paper/PaperWorkspace";
import { ResourceStore } from "../../src/core/resourceStore";
import { RefreshRuntimeContext } from "../../src/core/useResource";
import "../../src/styles.css";

const store = new ResourceStore();
const project = {
  id: "p",
  revision: 1,
  agent_profiles: {
    paper_coach: { provider: "codex", model: "", reasoning: "medium", run_on: "local" },
  },
  provider_readiness: {},
  machines: [],
};
createRoot(document.getElementById("root")!).render(
  <RefreshRuntimeContext.Provider value={{ store } as never}>
    <PaperWorkspace
      project={project as never}
      apiBase="/api/projects/p"
      tasks={[]}
      onStartTask={async () => {
        throw new Error("unused");
      }}
    />
  </RefreshRuntimeContext.Provider>,
);
