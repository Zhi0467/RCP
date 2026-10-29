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
// A test's init script may seed coach turns and record the turns the coach starts.
const coach = (window as { paperCoachFixture?: { tasks: unknown[]; started: unknown[] } })
  .paperCoachFixture;
createRoot(document.getElementById("root")!).render(
  <RefreshRuntimeContext.Provider value={{ store } as never}>
    <PaperWorkspace
      project={project as never}
      apiBase="/api/projects/p"
      tasks={(coach?.tasks ?? []) as never}
      onStartTask={async (kind, request) => {
        if (!coach) throw new Error("unused");
        coach.started.push({ kind, request });
        const turn = coach.tasks[0] as Record<string, unknown>;
        return {
          ...turn,
          operation_id: `started-${coach.started.length}`,
          request,
          result: null,
        } as never;
      }}
    />
  </RefreshRuntimeContext.Provider>,
);
