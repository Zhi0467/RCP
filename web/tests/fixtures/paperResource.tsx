import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { PaperWorkspace, type PaperEditorState } from "../../src/paper/PaperWorkspace";
import { paperFileRequest } from "../../src/paper/paperResources";
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

function Tab() {
  const [shown, setShown] = useState(true);
  const [request, setRequest] = useState(() => paperFileRequest(window.location.hash));
  const remembered = useRef<PaperEditorState | undefined>(undefined);
  useEffect(() => {
    const changed = () => setRequest(paperFileRequest(window.location.hash));
    window.addEventListener("hashchange", changed);
    (window as { paperTab?: unknown }).paperTab = {
      leave: () => setShown(false),
      back: () => setShown(true),
    };
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  if (!shown) return null;
  return (
    <PaperWorkspace
      project={project as never}
      apiBase="/api/projects/p"
      editorState={remembered.current}
      requestedFileId={request}
      onEditorStateChange={(state) => {
        remembered.current = state;
      }}
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
  );
}

createRoot(document.getElementById("root")!).render(
  <RefreshRuntimeContext.Provider value={{ store } as never}>
    <Tab />
  </RefreshRuntimeContext.Provider>,
);
