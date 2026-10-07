import assert from "node:assert/strict";
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
