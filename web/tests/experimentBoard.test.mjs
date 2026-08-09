import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const {
  buildExperimentBoard,
  experimentBoardHref,
  experimentTerminalLabel,
  parseProjectHash,
  projectHashAfterViewChange,
} = await server.ssrLoadModule("/src/experimentBoard.ts");
const { ExperimentBoard } = await server.ssrLoadModule("/src/components/ExperimentBoard.tsx");

after(() => server.close());

function node(id, status = "planned", updatedRev = 1) {
  return {
    id,
    type: "experiment",
    title: `Experiment ${id}`,
    extension_fields: {},
    standing: "asserted",
    created_rev: 1,
    updated_rev: updatedRev,
    source_refs: [],
    status,
    attempts: [],
  };
}

function control(fields = {}, operationalFields = {}) {
  return {
    ready: true,
    reasons: [],
    invocations_used: 1,
    invocation_ceiling: 3,
    invocations_remaining: 2,
    episode_id: "episode-1",
    paused: false,
    active: false,
    governing_decisions: [],
    decision_drift: [],
    operational: {
      task_active: false,
      detached_work_active: false,
      watcher_degraded: false,
      watcher_completion_pending: false,
      episode_exited: false,
      stop_requested: false,
      stop_settled: false,
      chat_id: "chat-1",
      current_operation_id: "operation-1",
      current_status: null,
      current_phase: null,
      current_status_message: null,
      current_last_activity_at: null,
      current_invocation: 1,
      session: {
        provider: "codex",
        model: null,
        reasoning: null,
        run_on: "local",
        execution_host: "local",
        run_truth_scope: null,
        native_session_bound: true,
        diagnostic: null,
      },
      ...operationalFields,
    },
    ...fields,
  };
}

function entry(id, nodeStatus, controlState, projectName = "Project") {
  return {
    project_id: `project-${projectName}`,
    project_name: projectName,
    project_reachable: true,
    node: node(id, nodeStatus),
    control: controlState,
  };
}

test("board reuses loop health and groups current state in operational order", () => {
  const board = buildExperimentBoard([
    entry(
      "stopped",
      "active",
      control({}, { current_status: "failed", stop_requested: true, stop_settled: true }),
    ),
    entry("running", "active", control({}, { current_status: "running" })),
    entry("degraded", "active", control({}, { watcher_degraded: true })),
    entry("finished", "completed", control()),
  ]);

  assert.deepEqual(
    board.needsAction.map((item) => [item.entry.node.id, item.health]),
    [["stopped", "human_stopped"]],
  );
  assert.deepEqual(
    board.inProgress.map((item) => [item.entry.node.id, item.health]),
    [
      ["degraded", "degraded"],
      ["running", "agent_active"],
    ],
  );
  assert.deepEqual(
    board.finished.map((item) => [item.entry.node.id, item.health]),
    [["finished", "completed"]],
  );
});

test("unsettled stops with actionable task states stay in Needs action exactly as Runs", () => {
  const board = buildExperimentBoard(
    ["failed", "paused", "interrupted"].map((status) =>
      entry(
        `stop-${status}`,
        "active",
        control({}, { current_status: status, stop_requested: true, stop_settled: false }),
      ),
    ),
  );

  assert.deepEqual(
    board.needsAction.map((item) => [item.entry.node.id, item.health]),
    [
      ["stop-failed", "stopping"],
      ["stop-interrupted", "stopping"],
      ["stop-paused", "stopping"],
    ],
  );
  assert.equal(board.inProgress.length, 0);
});

test("each section sorts by newest activity with a deterministic fallback", () => {
  const board = buildExperimentBoard([
    entry(
      "older",
      "active",
      control({}, { current_status: "running", current_last_activity_at: "2026-08-08T10:00:00Z" }),
      "Zulu",
    ),
    entry(
      "newer",
      "active",
      control({}, { current_status: "running", current_last_activity_at: "2026-08-09T10:00:00Z" }),
      "Zulu",
    ),
    entry("fallback-b", "active", control({}, { current_status: "running" }), "Beta"),
    entry("fallback-a", "active", control({}, { current_status: "running" }), "Alpha"),
  ]);

  assert.deepEqual(
    board.inProgress.map((item) => item.entry.node.id),
    ["newer", "older", "fallback-a", "fallback-b"],
  );
});

test("finished outcome labels stay distinct", () => {
  assert.equal(experimentTerminalLabel("completed"), "Succeeded");
  assert.equal(experimentTerminalLabel("abandoned"), "Abandoned");
  assert.equal(experimentTerminalLabel("superseded"), "Superseded");
});

test("experiment links round-trip through the project hash parser", () => {
  const href = experimentBoardHref("remote project/one", "experiment/alpha beta");
  assert.equal(
    href,
    "#/projects/remote%20project%2Fone?view=runs&experiment=experiment%2Falpha%20beta",
  );
  assert.deepEqual(parseProjectHash(href), {
    projectId: "remote project/one",
    view: "execution",
    experimentId: "experiment/alpha beta",
  });
  assert.deepEqual(parseProjectHash("#/projects/remote%20project%2Fone"), {
    projectId: "remote project/one",
    view: "overview",
    experimentId: null,
  });
  assert.deepEqual(parseProjectHash("#/projects/new"), {
    projectId: null,
    view: "overview",
    experimentId: null,
  });
  assert.equal(projectHashAfterViewChange(href, "overview"), "#/projects/remote%20project%2Fone");
  assert.equal(projectHashAfterViewChange(href, "execution"), null);
  assert.equal(projectHashAfterViewChange("#/projects/project-one", "attention"), null);
});

test("the rendered board keeps finished work folded and unavailable work explicit", () => {
  const entries = [
    {
      ...entry("needs-human", "active", control({}, { current_status: "failed" })),
      project_reachable: false,
      node: {
        ...node("needs-human", "active"),
        next_action: "Choose the recovery path.",
        current_summary: "An older summary.",
      },
    },
    entry("done", "superseded", control()),
  ];
  const html = renderToStaticMarkup(
    React.createElement(ExperimentBoard, { entries, onOpen: () => undefined }),
  );

  assert.match(html, /<h2 id="experiment-board-title">Experiments<\/h2>/);
  assert.match(html, /<details class="experiment-board-finished">/);
  assert.doesNotMatch(html, /<details[^>]+open/);
  assert.match(html, /Superseded/);
  assert.match(html, /Unavailable/);
  assert.match(html, /Choose the recovery path\./);
  assert.doesNotMatch(html, /An older summary\./);
  assert.doesNotMatch(html, />Run<|>Retry<|>Stop</);
});
