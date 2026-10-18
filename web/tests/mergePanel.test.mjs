import assert from "node:assert/strict";
import test from "node:test";

import { api } from "../src/core/api.ts";
import {
  mergePathMark,
  branchDiffWord,
  mergeDiffMarks,
  mergeRequestBody,
  previewAnswersDraft,
  unfinishedJobsFromError,
} from "../src/graph/mergePanel.ts";

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

test("an entity carries its most severe path without replacing review with needs_agent", () => {
  const paths = [
    path("a", { needs_proposal: true, needs_agent: true }),
    path("a", { conflict: true, needs_agent: true }),
    path("b", { delivered: true }),
    path("c"),
    path("d", { needs_proposal: true, needs_agent: true }),
    path("a", { entity: "edge", delivered: true }),
  ];
  assert.deepEqual(
    [...mergeDiffMarks(paths)],
    [
      ["node:a", "conflict"],
      ["node:b", "delivered"],
      ["node:c", "changed"],
      ["node:d", "proposal"],
      ["edge:a", "delivered"],
    ],
  );
  assert.equal(
    mergePathMark(path("e", { conflict: true, needs_proposal: true, needs_agent: true })),
    "conflict",
  );
  for (const change of ["created", "updated", "removed"]) {
    assert.equal(branchDiffWord(change, "changed"), change);
    assert.equal(branchDiffWord(change, undefined), change);
    assert.equal(branchDiffWord(change, "proposal"), "proposal");
    assert.equal(branchDiffWord(change, "delivered"), "delivered");
  }
});

test("a merge that needs an agent never squashes, and squash never keeps the branch open", () => {
  const choices = {
    targetBranch: " main ",
    historyMode: "squash",
    keepBranchOpen: true,
    removeWorktree: false,
    deleteCodeBranch: true,
  };
  const agentless = {
    graph: { direct: 0, conflicts: 0, reviews: 0, paths: [] },
    code,
    needs_agent: false,
  };
  assert.deepEqual(mergeRequestBody(agentless, choices, true), {
    history_mode: "squash",
    keep_branch_open: false,
    archive_graph_branch: true,
    remove_worktree: false,
    delete_code_branch: false,
    target_branch: "main",
  });
  for (const graph of [
    { direct: 2, conflicts: 1, reviews: 0, paths: [] },
    { direct: 2, conflicts: 0, reviews: 1, paths: [] },
  ]) {
    const agent = { ...agentless, graph, needs_agent: true };
    const body = mergeRequestBody(agent, choices, true);
    assert.equal(body.history_mode, "merge");
    assert.equal(body.keep_branch_open, true);
    assert.equal(body.archive_graph_branch, false);
  }
});

test("Merge waits for the preview of the target the human typed", () => {
  const preview = { code, graph: { direct: 0, conflicts: 0, reviews: 0, paths: [] } };
  assert.ok(previewAnswersDraft(preview, null, null, "main"));
  // Typed but not yet previewed, and previewed for an older target.
  assert.ok(!previewAnswersDraft(preview, null, null, "release"));
  assert.ok(!previewAnswersDraft(preview, null, "release", "release"));
  const release = { ...preview, code: { ...code, target_branch: "release" } };
  assert.ok(previewAnswersDraft(release, "release", "release", "release"));
});

async function refusal(detail) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
  try {
    await api("/api/merge");
  } catch (error) {
    return error;
  } finally {
    globalThis.fetch = originalFetch;
  }
  throw new Error("expected a refusal");
}

test("a Merge paused on unfinished jobs is told apart from other refusals", async () => {
  const jobs = [{ kind: "watcher", id: "w", status: "stopped" }];
  const code = "unfinished_jobs_confirmation_required";
  const paused = await refusal({ code, message: code, jobs });
  assert.deepEqual(unfinishedJobsFromError(paused), jobs);
  assert.equal(unfinishedJobsFromError(await refusal({ code: "target_dirty" })), null);
  assert.equal(unfinishedJobsFromError(await refusal("plain")), null);
});
