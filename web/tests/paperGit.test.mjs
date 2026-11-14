import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError } from "../src/core/api.ts";
import {
  decidePaperGit,
  deployKeyUrl,
  gitPill,
  paperGit,
  repositoryFromInput,
  validRepository,
} from "../src/paper/gitSync.ts";

const status = {
  state: "linked",
  repository: "lab/paper",
  branch: "main",
  public_key: "ssh-ed25519 AAAA",
  head: { commit: "abc", author: "Lena" },
  unpushed: true,
  conflicts: 2,
  skipped: [],
  last_error: "",
  checked_at: "2026-11-09T10:00:00Z",
};

test("the pill names the branch only for a linked paper", () => {
  assert.deepEqual(gitPill(status), { branch: "main", failed: false, hover: status.checked_at });
  assert.equal(gitPill({ ...status, state: "pending" }).branch, "");
  assert.deepEqual(gitPill(null), { branch: "", failed: false, hover: "" });
});
test("the pill hover prefers the last failure over the sync time", () => {
  const pill = gitPill({ ...status, last_error: "Permission denied" });
  assert.equal(pill.failed, true);
  assert.equal(pill.hover, "Permission denied");
});
test("a repository is owner/name with GitHub's characters", () => {
  for (const good of ["lab/paper", "a-b/c.d_e", "0x/1"]) assert.equal(validRepository(good), true);
  for (const bad of [
    "",
    "paper",
    "lab/",
    "/paper",
    "lab/paper/extra",
    "git@github.com:lab/paper.git",
    "-lab/paper",
    "lab/ paper",
  ])
    assert.equal(validRepository(bad), false);
  assert.equal(deployKeyUrl("lab/paper"), "https://github.com/lab/paper/settings/keys/new");
});
test("a pasted GitHub URL becomes owner/name", () => {
  for (const input of [
    "lab/paper",
    " https://github.com/lab/paper ",
    "https://github.com/lab/paper.git",
    "http://www.github.com/lab/paper/",
    "github.com/lab/paper",
    "git@github.com:lab/paper.git",
  ])
    assert.equal(repositoryFromInput(input), "lab/paper");
  assert.equal(
    validRepository(repositoryFromInput("https://github.com/lab/paper/tree/main")),
    false,
  );
});
test("requests reach the project's Git routes and keep a refusal's reason", async (t) => {
  const calls = [];
  let reply = () => new Response(JSON.stringify(status));
  t.mock.method(globalThis, "fetch", async (path, init) => {
    calls.push([path, init.method, init.body]);
    return reply();
  });
  assert.deepEqual(await paperGit("/api/projects/p", ""), status);
  await paperGit("/api/projects/p", "/key", "POST", { repository: "lab/paper", branch: "main" });
  await decidePaperGit("/api/projects/p", "g1", "accept");
  await decidePaperGit("/api/projects/p", "g1", "refresh");
  assert.deepEqual(calls, [
    ["/api/projects/p/paper/git", "GET", undefined],
    ["/api/projects/p/paper/git/key", "POST", '{"repository":"lab/paper","branch":"main"}'],
    ["/api/projects/p/paper/git/conflicts/decide", "POST", '{"group_id":"g1","decision":"accept"}'],
    ["/api/projects/p/paper/git/conflicts/refresh", "POST", undefined],
  ]);
  assert.throws(() => decidePaperGit("/api/projects/p", "g1", "undo"));
  reply = () => new Response(JSON.stringify({ reason: "conflicts" }), { status: 409 });
  await assert.rejects(
    paperGit("/api/projects/p", "/push", "POST", { message: "m" }),
    (error) => error instanceof ApiError && error.status === 409 && error.message === "conflicts",
  );
  reply = () => new Response(null, { status: 204 });
  assert.equal(await paperGit("/api/projects/p", "", "DELETE"), undefined);
});
