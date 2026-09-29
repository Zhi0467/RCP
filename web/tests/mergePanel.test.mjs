import assert from "node:assert/strict";
import test from "node:test";

import {
  mergeDiffCounts,
  mergeDiffMarks,
  mergeRequestBody,
  previewAnswersDraft,
  unfinishedJobsFromError,
} from "../src/mergePanel.ts";

function path(id, flags = {}) {
  return {
    entity: "node",
    id,
    field_path: "title",
    base: null,
    branch: null,
    main: null,
    delivered: false,
    conflict: false,
    needs_agent: false,
    needs_proposal: false,
    residue_reason: null,
    ...flags,
  };
}

const code = {
  repo_alias: "repo",
  source_branch: "rcp/episode-x",
  target_branch: "main",
  commits_ahead: 1,
  leftover_files: [],
  status: "clean",
  conflict_files: [],
};

test("an entity carries its most severe path, and counts are per entity", () => {
  const paths = [
    path("a", { needs_proposal: true }),
    path("a", { conflict: true }),
    path("b", { delivered: true }),
    path("c"),
  ];
  assert.equal(mergeDiffMarks(paths).get("node:a"), "conflict");
  assert.deepEqual(mergeDiffCounts(paths), {
    changed: 2,
    delivered: 1,
    proposals: 0,
    conflicts: 1,
    needsAgent: 1,
  });
});

test("a merge that needs an agent never squashes, and squash never keeps the branch open", () => {
  const choices = {
    targetBranch: " main ",
    historyMode: "squash",
    keepBranchOpen: true,
    removeWorktree: false,
    deleteCodeBranch: true,
  };
  const agentless = { graph: { ops: 0, residue: [], paths: [] }, code, needs_agent: false };
  assert.deepEqual(mergeRequestBody(agentless, choices, true), {
    history_mode: "squash",
    keep_branch_open: false,
    archive_graph_branch: true,
    remove_worktree: false,
    delete_code_branch: false,
    target_branch: "main",
  });
  const agent = { ...agentless, needs_agent: true };
  const body = mergeRequestBody(agent, choices, true);
  assert.equal(body.history_mode, "merge");
  assert.equal(body.keep_branch_open, true);
  assert.equal(body.archive_graph_branch, false);
});

test("Merge waits for the preview of the target the human typed", () => {
  const preview = { code, graph: { paths: [] } };
  assert.ok(previewAnswersDraft(preview, null, null, "main"));
  // Typed but not yet previewed, and previewed for an older target.
  assert.ok(!previewAnswersDraft(preview, null, null, "release"));
  assert.ok(!previewAnswersDraft(preview, null, "release", "release"));
  const release = { ...preview, code: { ...code, target_branch: "release" } };
  assert.ok(previewAnswersDraft(release, "release", "release", "release"));
});

test("a Merge paused on unfinished jobs is told apart from other refusals", () => {
  const jobs = [{ kind: "watcher", id: "w", status: "stopped" }];
  const paused = new Error(JSON.stringify({ code: "unfinished_jobs_confirmation_required", jobs }));
  assert.deepEqual(unfinishedJobsFromError(paused), jobs);
  assert.equal(unfinishedJobsFromError(new Error(JSON.stringify({ code: "target_dirty" }))), null);
  assert.equal(unfinishedJobsFromError(new Error("plain")), null);
});
