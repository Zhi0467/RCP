import type { ProjectSnapshot } from "../../src/core/types";

/** A minimal Project settings snapshot; beta restores a different ceiling than alpha. */
export function projectFor(id: string): ProjectSnapshot {
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
    agent_profiles: {
      ...Object.fromEntries(
        ["seed", "node_chat", "project_chat", "paper_coach", "orchestrator"].map((kind) => [
          kind,
          profile,
        ]),
      ),
      // Saved before Seed and Refresh shared one profile, so the two differ.
      refresh: { ...profile, reasoning: "high" },
    },
    providers: { codex: readiness },
    provider_readiness: { local: { codex: readiness } },
    repositories: [
      { alias: "repo", machine: "local", path: `/${id}/repo` },
      { alias: "data", machine: "local", path: `/${id}/data` },
    ],
    project_truth_scope: ["repo"],
    default_auto_research_invocation_ceiling: id === "beta" ? 20 : 10,
    state_repository: "repo",
    machines: [
      {
        machine_id: "local",
        compute: null,
        compute_probes: { scheduler: null, helper: null },
        removable: false,
      },
    ],
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
