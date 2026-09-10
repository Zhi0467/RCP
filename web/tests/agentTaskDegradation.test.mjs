import assert from "node:assert/strict";
import { withTaskAnswers } from "./taskAnswers.mjs";
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
const { AgentTaskInspector } = await server.ssrLoadModule("/src/components/AgentTaskInspector.tsx");
const { ProjectHistoryDrawer } = await server.ssrLoadModule(
  "/src/components/ProjectHistoryDrawer.tsx",
);

after(() => server.close());

// The backend authors this sentence. The web layer must show it and not reword
// it, so the tests match the string itself rather than a paraphrase.
const NOTE = "Claude ignored the requested reasoning effort 'ultra' and ran at its own default.";

function task(degradation) {
  const now = "2026-08-03T12:00:00Z";
  return withTaskAnswers({
    operation_id: "task-degraded",
    project_id: "project",
    kind: "seed",
    status: "succeeded",
    request: { provider: "claude", run_on: "local" },
    created_at: now,
    updated_at: now,
    status_message: "Task status",
    degradation,
    attempt: 1,
    estimate_seconds: 300,
    estimate_samples: 1,
    phase: "agent",
    elapsed_seconds: 10,
    progress: 1,
    can_pause: false,
    can_resume: false,
    can_retry: false,
    settled: true,
    events: [],
  });
}

function inspector(degradation) {
  const selected = task(degradation);
  return renderToStaticMarkup(
    React.createElement(AgentTaskInspector, {
      tasks: [selected],
      task: selected,
      loading: false,
      actionBusy: false,
      onSelect() {},
      onPause() {},
      onResume() {},
      onRetry() {},
      onClose() {},
    }),
  );
}

function drawer(degradation) {
  return renderToStaticMarkup(
    React.createElement(ProjectHistoryDrawer, {
      projectId: "project",
      summaries: [],
      tasks: [task(degradation)],
      loading: false,
      error: null,
      onInspectTask() {},
      episodeReportHref: () => "#",
      onClose() {},
    }),
  );
}

test("a succeeded task that lost part of its launch says so where it is listed", () => {
  // The turn succeeded, so status alone reads as untroubled; the note is the
  // only thing on the row that says otherwise.
  assert.match(drawer(NOTE), /ignored the requested reasoning effort/);
  assert.match(inspector(NOTE), /ignored the requested reasoning effort/);
});

test("the inspector detail repeats the note beside the status it contradicts", () => {
  const html = inspector(NOTE);

  assert.match(html, /run-degradation/);
  assert.equal(html.match(/ignored the requested reasoning effort/g).length, 2);
});

test("an untroubled task claims nothing", () => {
  for (const value of [null, undefined, ""]) {
    assert.doesNotMatch(inspector(value), /run-degradation|ignored the requested/);
    assert.doesNotMatch(drawer(value), /run-history-degraded/);
  }
});
