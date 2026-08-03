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
const { AttentionOverview, ExecutionView } = await server.ssrLoadModule(
  "/src/views/GraphViews.tsx",
);
const { ProjectSettings } = await server.ssrLoadModule("/src/views/ProjectSettings.tsx");

after(() => server.close());

function graph(overrides = {}) {
  return {
    revision: 1,
    nodes: {},
    edges: {},
    proposals: {},
    ambiguities: {},
    glossary: {},
    validation_messages: [],
    belief_transitions: [],
    replay_status: "complete",
    replay_failure: null,
    ontology: { types: [], fields: [], relations: [] },
    ...overrides,
  };
}

function blocker(id, blockerType, status = "open") {
  return {
    id,
    type: "blocker",
    title: id,
    blocker_type: blockerType,
    status,
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  };
}

function task(operationId, kind, status, statusMessage, updatedAt = "2026-08-03T00:00:00Z") {
  return {
    operation_id: operationId,
    project_id: "project",
    kind,
    status,
    request: {},
    created_at: updatedAt,
    updated_at: updatedAt,
    status_message: statusMessage,
    attempt: 1,
    estimate_seconds: 60,
    estimate_samples: 1,
    phase: status,
    elapsed_seconds: 1,
    progress: 0.1,
    can_pause: false,
    can_resume: false,
    can_retry: false,
  };
}

test("Inbox summary counts every open blocker and labels the tile plainly", () => {
  const html = renderToStaticMarkup(
    React.createElement(AttentionOverview, {
      graph: graph({
        nodes: {
          scientific: blocker("Scientific", "scientific"),
          infrastructure: blocker("Infrastructure", "infrastructure"),
          closed: blocker("Closed", "design", "resolved"),
        },
        proposals: {
          pending: {
            id: "pending",
            title: "Pending",
            card: {},
            ops: [],
            related_node_ids: [],
            base_rev: 1,
            status: "pending",
          },
        },
        ambiguities: {
          open: {
            id: "open",
            question: "Open ambiguity",
            why_it_matters: "It matters",
            related_node_ids: [],
            status: "open",
          },
        },
      }),
      onSelectNode() {},
    }),
  );

  assert.match(html, /4 open/);
  assert.match(html, /Open blockers<\/span><strong>2<\/strong>/);
  assert.doesNotMatch(html, /Scientific blockers/);
});

test("Runs projects Seed and Refresh tasks but excludes every chat surface", () => {
  const html = renderToStaticMarkup(
    React.createElement(ExecutionView, {
      graph: graph(),
      trustView: "working",
      tasks: [
        task("refresh", "refresh", "failed", "REFRESH FAILURE", "2026-08-03T01:00:00Z"),
        task("seed", "seed", "succeeded", "SEED COMPLETE"),
        task("node-chat", "node_chat", "failed", "NODE CHAT TRACEBACK"),
        task("project-chat", "project_chat", "running", "PROJECT CHAT RUNNING"),
        task("coach", "paper_coach", "failed", "PAPER COACH FAILURE"),
      ],
      dismissedTaskIds: new Set(),
      lastRefreshAt: null,
      onInspectTask() {},
      onDismissTask() {},
      onSelectNode() {},
    }),
  );

  assert.match(html, /REFRESH FAILURE/);
  assert.match(html, /SEED COMPLETE/);
  assert.doesNotMatch(html, /NODE CHAT TRACEBACK|PROJECT CHAT RUNNING|PAPER COACH FAILURE/);
});

test("Project Settings has no ontology authoring surface", () => {
  const storage = new Map();
  const previousLocalStorage = globalThis.localStorage;
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  try {
    const permissions = {
      read_graph: true,
      read_research_md: true,
      read_introduction: false,
      read_repositories: "run_scope",
      read_conversations: "none",
      write_graph_patch: false,
      write_project_files: false,
      write_paper: false,
    };
    const profile = {
      provider: "codex",
      model: "",
      reasoning: "medium",
      run_on: "local",
      permissions,
    };
    const metric = {
      bytes: 0,
      count: 0,
      limits: { max_bytes: 1, max_count: 1, ttl_seconds: 1 },
      reclaimable_bytes: 0,
      reclaimable_count: 0,
    };
    const html = renderToStaticMarkup(
      React.createElement(ProjectSettings, {
        apiBase: "/api/projects/project",
        project: {
          id: "project",
          name: "Project",
          state_repository: "repo",
          run_on: "local",
          default_run_truth_scope: ["repo"],
          repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
          machines: [{ alias: "local", host: "", provider_paths: { codex: "codex" } }],
          agent_profiles: {
            seed: profile,
            refresh: profile,
            node_chat: profile,
            project_chat: profile,
            paper_coach: profile,
          },
          providers: {},
          provider_readiness: {},
          cache_metrics: { remote_sources: metric, session_slices: metric },
        },
        usage: null,
        onRefreshUsage: async () => {},
        cacheClearDisabled: false,
        onSaved() {},
        onCacheMetricsChange() {},
        onRefreshReadiness: async () => {},
        showDisplaySettings: false,
        textScale: 100,
        onTextScaleChange() {},
      }),
    );

    assert.match(html, /Project boundary/);
    assert.match(html, /Agent defaults/);
    assert.doesNotMatch(html, /Ontology|Add node type|Add field|Add relation/);
  } finally {
    globalThis.localStorage = previousLocalStorage;
  }
});
