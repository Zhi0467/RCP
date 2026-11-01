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
after(() => server.close());
const { ConsolidationInbox } = await server.ssrLoadModule("/src/projects/ConsolidationInbox.tsx");

const item = {
  run_id: "run",
  kind: "failure",
  state: "open",
  occurrence_date: "2026-10-31",
  graph_branch_name: "main",
  operation_id: "operation",
  report: null,
  applied_revisions: [],
  revisions_verified: true,
  proposals_created: 0,
  error: { code: "report_missing", message: "The report was not produced." },
};

function render(row) {
  return renderToStaticMarkup(
    React.createElement(ConsolidationInbox, {
      items: [row],
      schedule: null,
      needsRenewal: false,
      error: null,
      writesDisabled: true,
      onOpenReport() {},
      onOpenTask() {},
      async onResolve() {},
      onOpenSettings() {},
    }),
  );
}

test("a report_missing failure offers its task inspector even while writes are disabled", () => {
  const html = render(item);
  assert.match(html, /The report was not produced/);
  assert.match(html, /<button[^>]*>.*?Open task<\/button>/);
  assert.doesNotMatch(html, /<button[^>]*disabled[^>]*>[^<]*.*?Open task/);
});

test("a historical failure without an operation has no task action", () => {
  assert.doesNotMatch(render({ ...item, operation_id: null }), /Open task/);
});

test("a report retains its report action", () => {
  const html = render({
    ...item,
    kind: "report",
    report: { title: "Report", artifact_id: "report" },
  });
  assert.doesNotMatch(html, /Open task/);
  assert.match(html, /> Open<\/button>/);
});
