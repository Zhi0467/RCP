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
    paper_coach: { provider: "codex", model: "", reasoning: "high", run_on: "local" },
    project_chat: {
      provider: "codex",
      model: "",
      reasoning: "medium",
      run_on: "local",
      effective_model: "",
      work_like_capable: true,
    },
  },
  provider_readiness: {
    local: {
      codex: {
        provider: "codex",
        label: "Codex",
        installed: true,
        authenticated: true,
        models: [],
      },
    },
  },
  repositories: [],
  project_truth_scope: [],
  run_on: "local",
  machines: [{ alias: "local", host: null }],
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
      coachActions={{
        actorId: "member",
        onResumeTask: () => undefined,
        onRetryTask: () => undefined,
        onRefreshTask: async (taskId) =>
          (coach?.tasks.find(
            (task) => (task as { operation_id: string }).operation_id === taskId,
          ) ?? null) as never,
        onInspectTask: () => undefined,
        onOpenInbox: () => undefined,
        onRepairGraphUpdate: async () => undefined,
      }}
      onStartTask={async (kind, request, requestId) => {
        if (!coach) throw new Error("unused");
        coach.started.push({ kind, request, requestId });
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
