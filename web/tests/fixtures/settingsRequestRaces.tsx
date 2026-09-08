import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ProjectSettings } from "../../src/views/ProjectSettings";
import type { IdentityResponse, ProjectSnapshot } from "../../src/types";
import "../../src/styles.css";

function projectFor(id: string): ProjectSnapshot {
  const profile = {
    provider: "codex",
    runtime: "exec",
    model: "",
    reasoning: "medium",
    run_on: "local",
    permissions: {},
  };
  const readiness = {
    provider: "codex",
    label: "Codex",
    installed: true,
    authenticated: true,
    binary_path: `/${id}/codex`,
    path_state: "resolved",
    models: [],
    runtimes: [{ id: "exec", label: "Exec" }],
    default_runtime: "exec",
  };
  return {
    id,
    name: id,
    run_on: "local",
    agent_profiles: Object.fromEntries(
      ["seed", "refresh", "node_chat", "project_chat", "paper_coach", "orchestrator"].map(
        (kind) => [kind, profile],
      ),
    ),
    providers: { codex: readiness },
    provider_readiness: { local: { codex: readiness } },
    repositories: [{ alias: "repo", machine: "local", path: `/${id}/repo` }],
    project_truth_scope: ["repo"],
    default_run_truth_scope: ["repo"],
    default_auto_research_invocation_ceiling: 10,
    state_repository: "repo",
    machines: [
      { alias: "local", host: null, provider_paths: { codex: `/${id}/codex` }, compute: null },
    ],
    compute_connections: [
      { id: "local", name: `${id} compute`, kind: "local", ssh_target: "", access_hint: "" },
    ],
    compute_status: {},
    cache_metrics: {
      remote_sources: {
        bytes: 1024,
        count: 1,
        limits: { max_bytes: 4096, max_count: 4, ttl_seconds: 3600 },
        reclaimable_bytes: 0,
        reclaimable_count: 0,
      },
      session_slices: {
        bytes: 2048,
        count: 1,
        limits: { max_bytes: 4096, max_count: 4, ttl_seconds: 3600 },
        reclaimable_bytes: 0,
        reclaimable_count: 0,
      },
    },
  } as unknown as ProjectSnapshot;
}

const publications: unknown[] = [];
Object.assign(window, { settingsProject: projectFor, settingsPublications: publications });
const teamIdentity: IdentityResponse | null = new URLSearchParams(window.location.search).has(
  "team",
)
  ? {
      space_id: "team",
      space_kind: "team",
      space_name: "Research team",
      user: {
        user_id: "self",
        display_name: "Researcher",
        identity_kind: "team_member",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        removal_started_at: null,
        removed_at: null,
      },
    }
  : null;

function Fixture() {
  const [project, setProject] = useState(() => projectFor("alpha"));
  const [visible, setVisible] = useState(true);
  return (
    <main>
      <header>
        <button onClick={() => setProject(projectFor("alpha"))}>Open alpha</button>
        <button onClick={() => setProject(projectFor("beta"))}>Open beta</button>
        <button onClick={() => setVisible((current) => !current)}>Toggle settings</button>
        <output aria-label="Current project">{project.id}</output>
      </header>
      {visible && (
        <ProjectSettings
          apiBase={`/api/projects/${project.id}`}
          project={project}
          identity={teamIdentity}
          onLeftProject={() => undefined}
          usage={null}
          onRefreshUsage={async () => undefined}
          cacheClearDisabled={false}
          onSaved={(saved) => {
            publications.push({ kind: "saved", projectId: saved.id });
            setProject((current) => (current.id === saved.id ? saved : current));
          }}
          onCacheMetricsChange={(metrics) => publications.push({ kind: "cache", metrics })}
          onRefreshReadiness={async () => undefined}
          showTextScale={false}
          spaceKind={teamIdentity ? "team" : "personal"}
          themeChoice="system"
          onThemeChoiceChange={() => undefined}
          textScale={100}
          onTextScaleChange={() => undefined}
        />
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
