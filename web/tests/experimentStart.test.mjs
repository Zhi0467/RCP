import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
import {
  canStartExperiment,
  experimentStartReasons,
  experimentStartOverlap,
} from "../src/experiments/experimentStart.ts";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const { ExperimentLoopMetadata } = await server.ssrLoadModule(
  "/src/experiments/ExperimentLoopMetadata.tsx",
);
const { ExperimentStartOverlap } = await server.ssrLoadModule(
  "/src/experiments/ExperimentStartOverlap.tsx",
);
const { startExperimentRun } = await server.ssrLoadModule("/src/core/api.ts");
const { parseProjectHash } = await server.ssrLoadModule("/src/experiments/experimentBoardModel.ts");
const main = { kind: "main", branch_id: null };
const branch = { kind: "branch", branch_id: "branch-one" };
const loop = {
  node_id: "experiment/one",
  episode_id: "other-loop",
  graph_target: { kind: "branch", branch_id: "parent-run" },
  started_by: { kind: "auto_research", human: null, auto_research_episode_id: "parent-run" },
  auto_research_parent_episode_id: "parent-run",
  checkout: {
    kind: "worktree",
    available: true,
    execution_host: "worker-host",
    repository_paths: ["/workspace/repo"],
    repository_alias: "repo",
    isolation_owner_episode_id: "different-owner",
  },
};

test("a new branch bypasses only source-target operational gates", () => {
  const live = {
    can_start: false,
    ready: false,
    node_closed: false,
    graph_reasons: [],
    isolated_start_reasons: [],
  };
  assert.equal(canStartExperiment(live, main, true), true);
  assert.equal(canStartExperiment(live, main, false), false);
  assert.equal(canStartExperiment(live, branch, true), false);
  assert.equal(
    canStartExperiment(
      { ...live, isolated_start_reasons: ["unresolved-prerequisite"] },
      main,
      true,
    ),
    false,
  );
  assert.equal(
    canStartExperiment(
      { ...live, node_closed: true, isolated_start_reasons: ["closed-node"] },
      main,
      true,
    ),
    false,
  );
  assert.equal(canStartExperiment(null, main, true), false);
  const gates = {
    ...live,
    reasons: ["runtime", "closed", "graph"],
    isolated_start_reasons: ["graph", "closed"],
  };
  assert.deepEqual(experimentStartReasons(gates, main, true), ["graph", "closed"]);
  assert.deepEqual(experimentStartReasons(gates, main), gates.reasons);
  assert.deepEqual(experimentStartReasons(gates, branch, true), gates.reasons);
});

test("starter metadata links the initiating parent independently of checkout ownership", () => {
  const render = (metadata) =>
    renderToStaticMarkup(
      React.createElement(ExperimentLoopMetadata, { projectId: "project-one", metadata }),
    );
  const html = render(loop);
  const href = html.match(/href="([^"]+)"/)[1].replaceAll("&amp;", "&");
  assert.equal(parseProjectHash(href).autoResearchEpisodeId, "parent-run");
  assert.match(html, /data-starter-kind="auto_research"/);
  assert.match(html, /data-checkout-kind="worktree"/);
  const checkoutTitle = html.match(/data-checkout-kind="worktree" title="([^"]+)"/)[1];
  assert.ok(checkoutTitle.includes(loop.checkout.execution_host));
  assert.ok(checkoutTitle.includes(loop.checkout.repository_paths[0]));
  const human = render({
    ...loop,
    started_by: { kind: "human", human: { display_name: "member-id" } },
    auto_research_parent_episode_id: null,
    checkout: { ...loop.checkout, kind: "shared" },
  });
  assert.match(human, /data-starter-kind="human"/);
  assert.match(human, /data-checkout-kind="shared"/);
  assert.doesNotMatch(human, /<a /);
});

test("the human start preserves and renders every overlap without adding a gate", async () => {
  const response = {
    operation_id: "new-task",
    episode_id: "new-loop",
    graph_target: branch,
    live_elsewhere: {
      omitted: 4,
      rows: [
        { ...loop, started_by: { kind: "auto_research", id: "parent-run" } },
        {
          ...loop,
          episode_id: "another-loop",
          graph_target: main,
          auto_research_parent_episode_id: null,
          started_by: { kind: "human", id: "private-user-id", display_name: "Recorded member" },
        },
      ],
    },
  };
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, init) => {
    requests.push({ path, init });
    return new Response(JSON.stringify(response), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const result = await startExperimentRun(
      "/api/projects/project-one/experiments/experiment%2Fone/run",
      { graph_isolation: true, code_worktree: false },
    );
    assert.deepEqual(result, response);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].init.method, "POST");
    assert.equal(JSON.parse(requests[0].init.body).graph_isolation, true);
    const html = renderToStaticMarkup(
      React.createElement(ExperimentStartOverlap, {
        projectId: "project-one",
        loops: result.live_elsewhere,
        onDismiss() {},
      }),
    );
    assert.deepEqual(
      [...html.matchAll(/data-overlap-episode-id="([^"]+)"/g)].map((match) => match[1]),
      ["other-loop", "another-loop"],
    );
    assert.match(html, /data-overlap-omitted="4"/);
    assert.ok(html.includes(response.live_elsewhere.rows[1].started_by.display_name));
    assert.ok(!html.includes(response.live_elsewhere.rows[1].started_by.id));
    assert.doesNotMatch(html, /disabled|role="alert"/);
    const routes = [...html.matchAll(/href="([^"]+)"/g)].map((match) =>
      parseProjectHash(match[1].replaceAll("&amp;", "&")),
    );
    assert.deepEqual(
      routes
        .filter((route) => route.experimentRoute)
        .map((route) => route.experimentRoute.episode_id),
      ["other-loop", "another-loop"],
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("starter attribution is omitted only for the displayed authorizing member", () => {
  const author = { space_id: "space-one", user_id: "member-one", display_name: "Member" };
  for (const [starter, expectedCount] of [
    [{ kind: "human", human: { ...author, display_name: "Old name" } }, 0],
    [{ kind: "human", human: { ...author, user_id: "member-two" } }, 1],
    [{ kind: "human", human: { ...author, space_id: "space-two" } }, 1],
    [loop.started_by, 1],
  ]) {
    const html = renderToStaticMarkup(
      React.createElement(ExperimentLoopMetadata, {
        projectId: "project-one",
        metadata: { ...loop, started_by: starter },
        author,
      }),
    );
    assert.equal([...html.matchAll(/data-starter-kind=/g)].length, expectedCount);
    assert.equal([...html.matchAll(/data-checkout-kind=/g)].length, 1);
  }
});

test("start inventory includes only live same-node overlaps, including main for a new branch", () => {
  const entry = (id, target, live = true, nodeId = loop.node_id, projectId = "project-one") => ({
    project_id: projectId,
    node: { id: nodeId },
    graph_target: target,
    control: { live },
    episode: { ...loop, episode_id: id },
  });
  const entries = [
    entry("main-loop", main),
    entry("branch-loop", branch),
    entry("ended", branch, false),
    entry("other-node", branch, true, "other"),
    entry("other-project", branch, true, loop.node_id, "other"),
  ];
  const ids = (target, isolated) =>
    experimentStartOverlap(entries, "project-one", loop.node_id, target, isolated).rows.map(
      (row) => row.episode_id,
    );
  assert.deepEqual(ids(main, false), ["branch-loop"]);
  assert.deepEqual(ids(main, true), ["main-loop", "branch-loop"]);
  assert.deepEqual(ids(branch, true), ["main-loop"]);
});

// Execute the production callbacks with their captured inputs, without mounting the whole App.
async function callbackFromSource(path, name, scope) {
  const source = ts.createSourceFile(
    path,
    await readFile(new URL(path, import.meta.url), "utf8"),
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  let callback;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === name) {
      callback = node.initializer.arguments[0];
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(callback, name);
  const { outputText } = ts.transpileModule(`return (${callback.getText(source)});`, {
    compilerOptions: { target: ts.ScriptTarget.ESNext },
  });
  return new Function(...Object.keys(scope), outputText)(...Object.values(scope));
}

test("the real start handler selects its returned isolated episode beside a live main loop", async () => {
  const { experimentStartTarget, sameGraphTarget, graphTargetUrl, graphTargetFromHash } =
    await server.ssrLoadModule("/src/core/graphTarget.ts");
  const { experimentBoardHref } = await server.ssrLoadModule(
    "/src/experiments/experimentBoardModel.ts",
  );
  const { reduceExperimentSelection } = await server.ssrLoadModule(
    "/src/graph/useGraphSelection.ts",
  );
  let selection = { selectedExperimentRunId: loop.node_id, selectedExperimentRoute: null };
  let view;
  let href;
  const dispatchExperimentSelection = (action) => {
    selection = reduceExperimentSelection(selection, action);
  };
  const replaceExactExperimentRoute = await callbackFromSource(
    "../src/graph/useGraphSelection.ts",
    "replaceExactExperimentRoute",
    {
      window: {
        location: { hash: "#/projects/project-one" },
        history: {
          replaceState(_state, _title, value) {
            href = value;
          },
        },
      },
      graphTargetUrl,
      graphTargetFromHash,
      experimentBoardHref,
      dispatchExperimentSelection,
    },
  );
  const showExperiment = await callbackFromSource(
    "../src/graph/useGraphSelection.ts",
    "showExperiment",
    {
      projectId: "project-one",
      replaceExactExperimentRoute,
      replaceExactRunExperimentSelection() {},
      dispatchExperimentSelection,
      setSelectedNode() {},
      setCompanionNode() {},
      changeView(value) {
        view = value;
      },
    },
  );
  const task = {
    episode_id: "new-isolated-loop",
    graph_target: { kind: "branch", branch_id: "new-isolated-loop" },
    live_elsewhere: { rows: [{ episode_id: "live-main-loop", graph_target: main }], omitted: 0 },
  };
  let finished = 0;
  const start = await callbackFromSource("../src/App.tsx", "startExperiment", {
    project: {
      id: "project-one",
      experiment_control: { [loop.node_id]: { can_start: false, isolated_start_reasons: [] } },
      agent_profiles: { node_chat: {} },
      default_run_truth_scope: [],
    },
    isControlNode: () => true,
    mutationsDisabled: false,
    experimentStartRequiresSync: false,
    canStartExperiment,
    graphTarget: main,
    beginTaskStart: () => () => {
      finished += 1;
    },
    window: { crypto: { randomUUID: () => "fresh-chat" } },
    runScope: [],
    apiBase: "/api/projects/project-one",
    graphPath: (path) => path,
    startExperimentRun: async () => task,
    experimentStartTarget,
    sameGraphTarget,
    isActiveGraph: () => true,
    recordStartedTask() {},
    setExperimentStartOverlap() {},
    setNotice() {},
    setFloatingChat() {},
    showExperiment,
    reload: async () => {},
    refreshEpisodes: async () => {},
  });
  assert.equal(
    await start({ id: loop.node_id, type: "experiment" }, undefined, { graph_isolation: true }),
    task,
  );
  assert.deepEqual(selection.selectedExperimentRoute, {
    experiment_id: loop.node_id,
    episode_id: task.episode_id,
    graph_target: task.graph_target,
    parent_episode_id: null,
  });
  assert.deepEqual(parseProjectHash(href).experimentRoute, selection.selectedExperimentRoute);
  assert.equal(view, "execution");
  assert.equal(finished, 1);
  // The same navigation path must retain a supplied parent, rather than infer one from the target.
  showExperiment(loop.node_id, {
    ...selection.selectedExperimentRoute,
    graph_target: { kind: "branch", branch_id: "parent-run" },
    parent_episode_id: "parent-run",
  });
  assert.equal(selection.selectedExperimentRoute.parent_episode_id, "parent-run");
  assert.deepEqual(parseProjectHash(href).experimentRoute, selection.selectedExperimentRoute);
});

test("checkout identity exposes the host and every repository on cards and overlaps", () => {
  for (const execution_host of ["worker-host", ""]) {
    const checkout = {
      ...loop.checkout,
      execution_host,
      repository_paths: ["/workspace/one", "/workspace/two"],
    };
    const metadata = renderToStaticMarkup(
      React.createElement(ExperimentLoopMetadata, {
        projectId: "project-one",
        metadata: { ...loop, checkout },
      }),
    );
    const overlap = renderToStaticMarkup(
      React.createElement(ExperimentStartOverlap, {
        projectId: "project-one",
        loops: { rows: [{ ...loop, checkout, started_by: { kind: "human" } }], omitted: 0 },
      }),
    );
    const title = (html) => html.match(/data-checkout-kind="worktree" title="([^"]+)"/)[1];
    assert.equal(title(metadata), title(overlap));
    const [host, ...paths] = title(overlap).split(" · ");
    assert.ok(host.length > 0);
    if (execution_host) assert.equal(host, execution_host);
    assert.deepEqual(paths, checkout.repository_paths);
  }
});

test("branch badges abbreviate ids, preserve full titles, and share overlap rendering", async () => {
  const { ExperimentBranchBadge } = await server.ssrLoadModule(
    "/src/experiments/ExperimentBranchBadge.tsx",
  );
  const id = "12345678-1234-1234-1234-123456789abc";
  const target = { kind: "branch", branch_id: id };
  const render = (autoResearchEpisodeId) =>
    renderToStaticMarkup(
      React.createElement(ExperimentBranchBadge, { target, autoResearchEpisodeId }),
    );
  const ordinary = render(null);
  const auto = render(id);
  assert.ok(ordinary.includes(`title="${id}"`));
  assert.ok(auto.includes(`title="${id}"`));
  const label = (html) => html.match(/<span>([^<]+)<\/span>/)[1];
  assert.equal(label(ordinary), id.slice(0, 8));
  assert.ok(label(auto).endsWith(id.slice(0, 8)));
  assert.ok(!label(auto).includes(id));
  assert.notEqual(label(auto), label(ordinary));
  assert.equal(render("different-parent"), ordinary);
  const html = renderToStaticMarkup(
    React.createElement(ExperimentStartOverlap, {
      projectId: "project-one",
      loops: {
        rows: [{ ...loop, graph_target: target, started_by: { kind: "auto_research", id } }],
        omitted: 0,
      },
    }),
  );
  assert.ok(html.includes(auto));
  assert.match(
    renderToStaticMarkup(React.createElement(ExperimentBranchBadge, { target: main })),
    /class="status-pill experiment-branch-badge"[^>]*data-graph-target-kind="main"/,
  );
});
