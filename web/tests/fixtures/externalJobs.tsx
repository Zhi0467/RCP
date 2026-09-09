import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ExperimentRunDetail } from "../../src/components/ExperimentRunDetail";
import { NodeChat } from "../../src/components/NodeChat";
import { buildExperimentRun } from "../../src/runProjection";
import { withExperimentControlAnswers } from "../taskAnswers.mjs";
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
const experiment = {
  id: "experiment-1",
  type: "experiment",
  title: "Watcher experiment",
  status: "running",
  invocation_ceiling: 5,
  extension_fields: {},
  source_refs: [],
};
const control = withExperimentControlAnswers({
  health: "waiting_on_watchers",
  recommendation: "wait",
  reasons: [],
  graph_reasons: [],
  episode_id: "episode-1",
  episode: null,
  invocations_used: 1,
  invocation_ceiling: 5,
  operational: { session: null },
});
const fixtureWatchers = [
  { watcher_id: "active", status: "active", can_cancel: true },
  { watcher_id: "completed", status: "completed", can_cancel: false },
  {
    watcher_id: "grouped",
    status: "completed",
    can_cancel: false,
    group_id: "finished-group",
    group_label: "Finished batch",
  },
].map((fields) => ({
  ...initialWatcher,
  ...fields,
  log_path: `/scratch/${fields.watcher_id}.log`,
  created_at: new Date().toISOString(),
  continuation: {
    patch_kind: "experiment_loop",
    control_node_id: experiment.id,
    control_episode_id: "episode-1",
  },
  delivery_label: "Not delivered",
}));

function WatcherFixture() {
  const [projectId, setProjectId] = useState("project");
  const [watchers, setWatchers] = useState(() =>
    new URLSearchParams(location.search).get("watchers") === "graph"
      ? fixtureWatchers.map(({ watcher_id, status, continuation, created_at }) => ({
          watcher_id,
          status,
          continuation,
          created_at,
          condition:
            watcher_id === "grouped"
              ? { node_id: "hypothesis/grouped", proposal_resolved: true }
              : { node_id: `decision/${watcher_id}`, status_in: ["decided"] },
          last_evaluated_at: created_at,
          delivery_label: "Not delivered",
        }))
      : fixtureWatchers,
  );
  Object.assign(window, { watcherFixture: { watchers, setWatchers, setProjectId } });
  const project = { ...initialProject, id: projectId };
  const run = buildExperimentRun(experiment as never, control, [], watchers as never);
  return (
    <main style={{ padding: 20 }}>
      <section aria-label="Experiment run">
        <ExperimentRunDetail
          apiBase={`/api/projects/${projectId}`}
          run={run}
          runBusy={false}
          runDisabled={false}
          stopBusy={false}
          recoveryBusy={false}
          watcherCheckBusyId={null}
          onRun={noop}
          onStopLoop={noop}
          onRecover={noop}
          onSwitchProvider={noop}
          onCheckWatcher={noop}
          episodeReportHref={() => "#report"}
        />
      </section>
      <NodeChat
        project={project as never}
        node={experiment as never}
        runScope={["repo"]}
        tasks={[]}
        watchers={watchers as never}
        chatId="watcher-chat"
        presentation="workspace"
        readOnly
        onStartTask={ready as never}
        onInspectTask={noop}
        onOpenInbox={noop}
        onRepairGraphUpdate={ready}
        onNewSession={noop}
        onClose={noop}
        onResumeTask={noop}
        onRetryTask={noop}
        onRefreshTask={ready as never}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  new URLSearchParams(location.search).has("watchers") ? <WatcherFixture /> : <Fixture />,
);
