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
const { RunDialog } = await server.ssrLoadModule("/src/components/RunDialog.tsx");
const { AgentTaskInspector } = await server.ssrLoadModule("/src/components/AgentTaskInspector.tsx");
const { providerPathPresentation } = await server.ssrLoadModule("/src/views/ProjectSettings.tsx");
const { ChatsWorkspace } = await server.ssrLoadModule("/src/views/ChatsWorkspace.tsx");

after(() => server.close());

const project = {
  agent_profiles: {
    seed: {
      provider: "codex",
      model: "",
      reasoning: "medium",
      run_on: "local",
      permissions: {},
    },
  },
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
  repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
  project_truth_scope: ["repo"],
  state_repository: "repo",
  machines: [{ alias: "local", host: null }],
};

test("seed and refresh runs offer one empty, labelled additional-message field", () => {
  const html = renderToStaticMarkup(React.createElement(RunDialog, {
    open: true,
    kind: "seed",
    project,
    initialScope: ["repo"],
    busy: false,
    onClose() {},
    onRun() {},
  }));

  assert.match(html, /<label[^>]*>\s*<span>Additional message \(optional\)<\/span>\s*<textarea rows="4"><\/textarea>/);
  assert.equal(html.match(/<textarea/g)?.length, 1);
  assert.doesNotMatch(html, /placeholder=/);
});

test("a closed run dialog renders no message field", () => {
  const html = renderToStaticMarkup(React.createElement(RunDialog, {
    open: false,
    kind: "seed",
    project,
    initialScope: ["repo"],
    busy: false,
    onClose() {},
    onRun() {},
  }));

  assert.equal(html, "");
});

test("chat history exposes one explicit end-of-list page control", () => {
  const common = {
    project,
    conversations: [],
    selectedChatId: null,
    nodes: {},
    runScope: [],
    tasks: [],
    activeTask: null,
    graphChangesDisabled: false,
    unreadTaskIds: new Set(),
    chatTranscripts: new Map(),
    onSelect() {},
    onLoadMore() {},
    onStartTask() {},
  };
  const ready = renderToStaticMarkup(React.createElement(ChatsWorkspace, {
    ...common,
    hasMore: true,
    loadingMore: false,
  }));
  const complete = renderToStaticMarkup(React.createElement(ChatsWorkspace, {
    ...common,
    hasMore: false,
    loadingMore: false,
  }));
  assert.match(ready, /<button class="button primary compact" type="button">Load more<\/button>/);
  assert.doesNotMatch(complete, /Load more/);
});

test("retry keeps the original task boundary and exposes provider configuration", () => {
  const html = renderToStaticMarkup(React.createElement(RunDialog, {
    open: true,
    kind: "seed",
    mode: "retry",
    project,
    initialScope: ["repo"],
    initialConfig: {
      provider: "codex",
      model: "",
      reasoning: "medium",
      run_on: "local",
    },
    busy: false,
    onClose() {},
    onRun() {},
  }));

  assert.match(html, /Retry seed/);
  assert.doesNotMatch(html, /Truth input subset/);
  assert.doesNotMatch(html, /Additional message/);
  assert.match(html, />\s*Retry<\/button>/);
});

test("task inspector names every provider launch by its continuation cause", () => {
  const now = new Date().toISOString();
  const promptReceipt = (receipt_id, continuation_cause) => ({
    receipt_id,
    operation_id: "task-1",
    created_at: now,
    tier: "diagnostic",
    category: "agent_prompt",
    payload: { prompt: "Open the contract.\n", continuation_cause, line_count: 1 },
  });
  const task = {
    operation_id: "task-1",
    project_id: "project-1",
    kind: "seed",
    status: "running",
    request: { provider: "codex", run_on: "local" },
    created_at: now,
    updated_at: now,
    status_message: "Correcting the graph update.",
    attempt: 1,
    estimate_seconds: 60,
    estimate_samples: 0,
    phase: "agent",
    elapsed_seconds: 10,
    progress: 0.2,
    can_pause: true,
    can_resume: false,
    can_retry: false,
    debug_receipts: [
      promptReceipt(1, "fresh"),
      promptReceipt(2, "correction"),
      promptReceipt(3, "resume"),
      promptReceipt(4, "handoff"),
    ],
    events: [],
  };
  const html = renderToStaticMarkup(React.createElement(AgentTaskInspector, {
    tasks: [task],
    task,
    loading: false,
    actionBusy: false,
    onSelect() {},
    onPause() {},
    onResume() {},
    onRetry() {},
    onClose() {},
  }));

  assert.match(html, /First attempt · 1/);
  assert.match(html, /Correcting prior failure · 2/);
  assert.match(html, /Continuing after interruption · 3/);
  assert.match(html, /Continuing in a new session · 4/);
});

test("provider path state distinguishes a stale recorded executable", () => {
  assert.deepEqual(
    providerPathPresentation({ path_state: "missing" }, "/old/codex", "/old/codex"),
    { label: "Recorded path missing", kind: "error" },
  );
  assert.deepEqual(
    providerPathPresentation({ path_state: "resolved" }, "/new/codex", "/old/codex"),
    { label: "Unsaved", kind: "pending" },
  );
  assert.deepEqual(
    providerPathPresentation({ path_state: "denied" }, "/protected/codex", "/protected/codex"),
    { label: "Recorded path unusable", kind: "error" },
  );
});
