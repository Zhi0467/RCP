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
const { AgentTaskActivity } = await server.ssrLoadModule("/src/components/AgentTaskActivity.tsx");
const { AgentTaskInspector } = await server.ssrLoadModule("/src/components/AgentTaskInspector.tsx");

after(() => server.close());

function task(status) {
  const now = "2026-08-03T12:00:00Z";
  return {
    operation_id: `task-${status}`,
    project_id: "project",
    kind: "seed",
    status,
    request: { provider: "codex", run_on: "local" },
    created_at: now,
    updated_at: now,
    status_message: status === "failed" ? "Provider failed" : "Task status",
    attempt: 1,
    estimate_seconds: 300,
    estimate_samples: 1,
    phase: "agent",
    elapsed_seconds: 10,
    progress: 0.03,
    can_pause: status === "running",
    can_resume: status === "paused",
    can_retry: status === "failed" || status === "interrupted",
    events: [],
  };
}

function renderActivity(status) {
  return renderToStaticMarkup(
    React.createElement(AgentTaskActivity, {
      task: task(status),
      actionBusy: false,
      onPause() {},
      onResume() {},
      onRetry() {},
      onInspect() {},
      onDismiss() {},
    }),
  );
}

function renderInspector(status) {
  const selectedTask = task(status);
  return renderToStaticMarkup(
    React.createElement(AgentTaskInspector, {
      tasks: [selectedTask],
      task: selectedTask,
      loading: false,
      actionBusy: false,
      onSelect() {},
      onPause() {},
      onResume() {},
      onRetry() {},
      onDismiss() {},
      onClose() {},
    }),
  );
}

test("active tasks show live progress in the activity card and inspector", () => {
  const activity = renderActivity("running");
  const inspector = renderInspector("running");

  assert.match(activity, /aria-label="Estimated agent progress"/);
  assert.match(activity, />3%<\/span>/);
  assert.match(activity, /about 5m left/);
  assert.match(inspector, /Estimated progress/);
  assert.match(inspector, />3%<\/strong>/);
  assert.match(inspector, /about 5m left/);
});

test("terminal tasks show no live progress in the activity card or inspector", () => {
  for (const status of ["failed", "succeeded", "interrupted", "paused"]) {
    for (const html of [renderActivity(status), renderInspector(status)]) {
      assert.doesNotMatch(html, /Estimated (?:agent )?progress/);
      assert.doesNotMatch(html, />3%<\/(?:span|strong)>/);
      assert.doesNotMatch(html, /about 5m left/);
    }
  }
});
