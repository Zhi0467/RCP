import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { ApiError } from "../../src/core/api";
import { GraphPicker } from "../../src/graph/GraphPicker";
import type { GraphRef } from "../../src/core/types";
import "../../src/styles.css";

const head = { target: { kind: "main" }, revision: 7, transition_id: null } as const;
const refs: GraphRef[] = [
  {
    kind: "main",
    name: "main",
    running: false,
    needs_you: false,
    archived: false,
    branch_id: null,
    episode_id: null,
    current_episode_id: null,
    created_at: null,
    base_head: null,
    head,
    merge_eligible: false,
    merge_blocked_reason: null,
    merge_state: null,
    latest_successful_merge: null,
    active_merge_task_id: null,
    merge_diagnostic: null,
  },
  ...[false, true].map(
    (owned) =>
      ({
        kind: "branch",
        name: owned ? "episode-branch" : "member-fork",
        branch_id: owned ? "episode" : "fork",
        episode_id: owned ? "episode" : null,
        current_episode_id: owned ? "episode" : null,
        mode: owned ? "auto_research" : null,
        running: false,
        needs_you: false,
        archived: false,
        created_at: "2026-10-21T00:00:00Z",
        parent_head: head,
        base_head: head,
        head,
        merge_eligible: true,
        merge_blocked_reason: null,
        merge_state: "unmerged",
        latest_successful_merge: null,
        active_merge_task_id: null,
        merge_diagnostic: null,
      }) as GraphRef,
  ),
];

function Harness() {
  const [view, setView] = useState("Overview");
  const [notice, setNotice] = useState("");
  return (
    <>
      <nav>
        <button onClick={() => setView("Inbox")}>Inbox</button>
      </nav>
      <GraphPicker
        refs={refs}
        activeRef={{ kind: "main" }}
        projectId="project"
        onSelect={() => {}}
        onRefUpdated={() => {}}
        onMerge={async (ref, requestId, confirmUnfinishedJobs) => {
          setNotice("Starting merge…");
          const response = await fetch(`/api/projects/project/graph-refs/${ref.branch_id}/merge`, {
            method: "POST",
            body: JSON.stringify({
              request_id: requestId,
              confirm_unfinished_jobs: confirmUnfinishedJobs,
            }),
          });
          if (!response.ok) {
            const { detail } = await response.json();
            throw new ApiError("Merge refused", response.status, detail);
          }
          setNotice("Merge is running");
        }}
      />
      <main>
        <h1>{view}</h1>
        <p role="status">{notice}</p>
      </main>
    </>
  );
}
createRoot(document.getElementById("root")!).render(<Harness />);
