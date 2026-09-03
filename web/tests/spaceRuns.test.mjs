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
const { SpaceRuns } = await server.ssrLoadModule("/src/components/SpaceRuns.tsx");
const { experimentBoardHref, parseProjectHash, spaceRunRouteToken } =
  await server.ssrLoadModule("/src/experimentBoard.ts");

after(() => server.close());

function run(fields = {}) {
  return {
    episode_id: "episode-1",
    project_id: "project-1",
    project_name: "Adaptation Lab",
    project_reachable: true,
    mode: "experiment_loop",
    title: "Measure transfer",
    graph_target: { kind: "main", branch_id: null },
    parent_episode_id: null,
    experiment_id: "experiment/transfer",
    started_at: "2026-09-02T12:00:00Z",
    last_activity_at: "2026-09-02T12:10:00Z",
    health_label: "Needs action",
    health_tone: "actionable",
    run_section: "needs_action",
    ...fields,
  };
}

test("space Runs mixes active modes and folds completed groups", () => {
  const entries = [
    run(),
    run({
      episode_id: "episode-2",
      mode: "auto_research",
      title: "Auto-research",
      experiment_id: null,
      health_label: "Active",
      health_tone: "running",
    }),
    run({
      episode_id: "episode-3",
      health_label: "Completed",
      health_tone: "completed",
      run_section: "completed",
    }),
    run({
      episode_id: "episode-4",
      mode: "auto_research",
      title: "Auto-research",
      experiment_id: null,
      health_label: "Stopped",
      health_tone: "stopped",
      run_section: "completed",
    }),
  ];

  const html = renderToStaticMarkup(React.createElement(SpaceRuns, { entries, onOpen() {} }));

  assert.match(html, /<h2 id="space-runs-title">Runs<\/h2>/);
  assert.match(html, /<h3>Needs Action<\/h3>/);
  assert.match(html, /<h3>Completed<\/h3>/);
  assert.match(html, /<strong>Experiment loop<\/strong>/);
  assert.match(html, /<strong>Auto-research<\/strong>/);
  assert.doesNotMatch(html, /current_summary|Recommended next step/);
});

test("an Experiment space run keeps its exact Runs route", () => {
  const entry = run({
    graph_target: { kind: "branch", branch_id: "episode-parent" },
    parent_episode_id: "episode-parent",
  });
  const href = experimentBoardHref(entry.project_id, spaceRunRouteToken(entry));
  assert.deepEqual(parseProjectHash(href).experimentRoute, {
    experiment_id: "experiment/transfer",
    episode_id: "episode-1",
    graph_target: { kind: "branch", branch_id: "episode-parent" },
    parent_episode_id: "episode-parent",
  });
});

test("an Auto-research space run opens the project Runs panel", () => {
  const entry = run({
    project_id: "project-b",
    mode: "auto_research",
    title: "Auto-research",
    experiment_id: null,
  });
  const route = spaceRunRouteToken(entry);

  assert.equal(experimentBoardHref(entry.project_id, route), "#/projects/project-b?view=runs");
});
