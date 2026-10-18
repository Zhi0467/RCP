import React from "react";
import { createRoot } from "react-dom/client";
import { ProjectHistoryDrawer } from "../../src/agents/ProjectHistoryDrawer.tsx";
import { createGraphFork } from "../../src/core/api.ts";
import "../../src/styles.css";
const target = new URLSearchParams(location.search).has("branch")
  ? { kind: "branch", branch_id: "source" }
  : { kind: "main" };
createRoot(document.getElementById("root")).render(
  React.createElement(ProjectHistoryDrawer, {
    projectId: "project",
    tasks: [],
    loading: false,
    error: null,
    summaries: [
      {
        from_revision: 6,
        to_revision: 7,
        kind: "apply",
        producer: "human",
        sentences: [],
        created_at: "2026-08-03T08:00:00Z",
      },
    ],
    reportError(message) {
      document.querySelector("[role=alert]").textContent = message;
    },
    onClose() {},
    onInspectTask() {},
    episodeReportHref: () => "",
    onFork: (revision, requestId) => createGraphFork("project", target, revision, requestId),
  }),
);
