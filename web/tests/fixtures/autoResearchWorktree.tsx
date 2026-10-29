import { ResourceRuntime } from "./resourceRuntime";
import { createRoot } from "react-dom/client";
import { AutoResearchDialog } from "../../src/experiments/AutoResearchDialog";
import type { ProjectSnapshot } from "../../src/core/types";
import "../../src/styles.css";

// `?writable=repo-a,repo-b` names the repositories the project's agents can write.
const writable = (new URLSearchParams(location.search).get("writable") ?? "")
  .split(",")
  .filter(Boolean);
const profile = {
  provider: "codex",
  model: "",
  reasoning: "medium",
  runtime: "codex_app_server",
  run_on: "local",
  effective_model: "",
  work_like_capable: true,
  permissions: {},
};
const project = {
  id: "project",
  name: "Project",
  agent_profiles: { orchestrator: profile, node_chat: profile, project_chat: profile },
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
  repositories: [
    { alias: "repo-a", machine: "local", path: "/repo-a" },
    { alias: "repo-b", machine: "local", path: "/repo-b" },
  ],
  project_truth_scope: writable,
  state_repository: "repo-a",
  machines: [{ alias: "local", host: null }],
} as unknown as ProjectSnapshot;

const authorized: unknown[][] = [];
Object.assign(window, { authorized });

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <AutoResearchDialog
      open
      busy={false}
      error={null}
      initialInvocationCeiling={3}
      project={project}
      onClose={() => undefined}
      onAuthorize={(...args) => authorized.push(args)}
    />
  </ResourceRuntime>,
);
