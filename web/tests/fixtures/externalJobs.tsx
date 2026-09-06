import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ExternalJobRow } from "../../src/components/ExternalJobRow";
import { ProjectSettings } from "../../src/views/ProjectSettings";
import "../../src/styles.css";

const profile = {
  provider: "codex",
  runtime: "exec",
  model: "",
  reasoning: "medium",
  run_on: "local",
};
const metric = {
  bytes: 0,
  count: 0,
  limits: { max_bytes: 1, max_count: 1, ttl_seconds: 1 },
  reclaimable_bytes: 0,
  reclaimable_count: 0,
};
const initialProject = {
  id: "project",
  name: "Project",
  state_repository: "repo",
  run_on: "local",
  default_run_truth_scope: ["repo"],
  project_truth_scope: ["repo"],
  default_auto_research_invocation_ceiling: 5,
  repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
  machines: [
    {
      alias: "local",
      host: "",
      os_account: "rcp",
      provider_paths: {},
      compute: { job_manager: null, jobs_root: "/old" },
      compute_probe: null,
    },
    {
      alias: "cluster",
      host: "cluster",
      os_account: "rcp",
      provider_paths: {},
      compute: { job_manager: null, jobs_root: "/cluster" },
      compute_probe: null,
    },
  ],
  agent_profiles: Object.fromEntries(
    ["seed", "refresh", "node_chat", "project_chat", "paper_coach", "orchestrator"].map((name) => [
      name,
      profile,
    ]),
  ),
  providers: {},
  provider_readiness: {},
  compute_connections: [],
  cache_metrics: { remote_sources: metric, session_slices: metric },
};
const initialWatcher = {
  watcher_id: "watcher-1",
  status: "stopped",
  check_command: "check job",
  log_path: "/scratch/train.log",
  cwd: "/scratch",
  cancel_command: "cancel job",
  can_cancel: true,
  cancel_requested_by: null,
  cancel_requested_at: null,
  cancel_error: null,
  last_checked_at: null,
  last_error: null,
};
const noop = () => {};
const ready = async () => {};

function Fixture() {
  const [project, setProject] = useState(initialProject);
  const [watcher, setWatcher] = useState(initialWatcher);
  Object.assign(window, {
    jobFixture: { project, watcher, setProject, setWatcher },
  });
  return (
    <main>
      <section className="chat-watcher-row" aria-label="External job">
        <ExternalJobRow apiBase="/api/projects/project" watcher={watcher as never} />
      </section>
      <ProjectSettings
        apiBase="/api/projects/project"
        project={project as never}
        identity={null}
        onLeftProject={noop}
        usage={null}
        onRefreshUsage={ready}
        cacheClearDisabled={false}
        onSaved={setProject as never}
        onCacheMetricsChange={noop}
        onRefreshReadiness={ready}
        showTextScale={false}
        themeChoice="light"
        onThemeChoiceChange={noop}
        textScale={100}
        onTextScaleChange={noop}
        spaceKind="personal"
      />
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<Fixture />);
