import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const { projectViewHash, graphViewHash } = await server.ssrLoadModule("/src/core/graphTarget.ts");
const { conversationHref } = await server.ssrLoadModule("/src/chat/chatWorkspace.ts");
const { parseProjectHash, projectHashAfterViewChange } = await server.ssrLoadModule(
  "/src/experiments/experimentBoardModel.ts",
);
const {
  graphPickerOptions,
  graphRefCanMerge,
  graphRefParentLabel,
  graphRefDepth,
  refNeedsYou,
  selectionAfterGraphSwitch,
} = await server.ssrLoadModule("/src/graph/graphPickerModel.ts");
const main = { kind: "main" };
const branch = { kind: "branch", branch_id: "branch /+" };

test("project hashes encode refs and optional selections with existing wire values", () => {
  assert.equal(
    projectViewHash("project /", main, "execution"),
    "#/projects/project%20%2F?view=runs",
  );
  assert.equal(
    projectViewHash("p", branch, "chats", { chatId: "chat /+" }),
    "#/projects/p?view=chats&chat=chat+%2F%2B&branch_id=branch+%2F%2B",
  );
  const hash = projectViewHash("p", branch, "execution", { autoResearchEpisodeId: "episode /+" });
  const params = new URLSearchParams(hash.split("?")[1]);
  assert.equal(params.get("episode"), "episode /+");
  assert.equal(parseProjectHash(hash).autoResearchEpisodeId, "episode /+");
  assert.equal(params.get("branch_id"), branch.branch_id);
  assert.equal(params.get("view"), "runs");
  assert.equal(projectViewHash("p", main, null), "#/projects/p");
  assert.equal(graphViewHash("p", branch), "#/projects/p?view=dag&branch_id=branch+%2F%2B");
  assert.equal(
    conversationHref("p", { chatId: "chat /+", graphTarget: branch }),
    "#/projects/p?view=chats&chat=chat+%2F%2B&branch_id=branch+%2F%2B",
  );
  assert.equal(conversationHref("p", { chatId: "c" }), "#/projects/p?view=chats&chat=c");
});

test("view-change delegation preserves bare main routes and branch routes", () => {
  assert.equal(projectHashAfterViewChange("#/projects/p?view=runs", "dag"), "#/projects/p");
  assert.equal(
    projectHashAfterViewChange("#/projects/p?view=paper", "terminals"),
    "#/projects/p?view=terminals",
  );
  assert.equal(projectHashAfterViewChange("#/projects/p?view=artifacts", "chats"), "#/projects/p");
  assert.equal(projectHashAfterViewChange("#/projects/p?view=chats", "dag"), null);
  assert.equal(
    projectHashAfterViewChange("#/projects/p?view=chats&branch_id=b", "execution"),
    "#/projects/p?view=runs&branch_id=b",
  );
});

test("picker orders main, running branches, then newest and retains only the active archived branch", () => {
  const make = (id, date, extra = {}) => ({
    kind: "branch",
    branch_id: id,
    name: id,
    created_at: date,
    running: false,
    archived: false,
    ...extra,
  });
  const old = make("old", "2026-01-01T00:00:00Z");
  const recent = make("recent", "2026-03-01T00:00:00Z");
  const running = make("running", "2026-02-01T00:00:00Z", { running: true });
  const runningOld = make("running-old", "2026-01-01T00:00:00Z", { running: true });
  const archived = make("archived", "2026-04-01T00:00:00Z", { archived: true });
  const otherArchived = make("other-archived", "2026-05-01T00:00:00Z", { archived: true });
  const m = { kind: "main", archived: false };
  const refs = [old, archived, runningOld, recent, m, otherArchived, running];
  const original = [...refs];
  assert.deepEqual(graphPickerOptions(refs, main), [m, running, runningOld, recent, old]);
  assert.deepEqual(graphPickerOptions(refs, { kind: "branch", branch_id: "archived" }), [
    m,
    running,
    runningOld,
    archived,
    recent,
    old,
  ]);
  assert.deepEqual(refs, original);
});

test("an archived branch never needs you", () => {
  const ref = { kind: "branch", branch_id: "b", needs_you: true, archived: false };
  assert.equal(refNeedsYou(ref), true);
  assert.equal(refNeedsYou({ ...ref, archived: true }), false);
  assert.equal(refNeedsYou({ kind: "main", needs_you: false }), false);
});

test("switch preserves view and only destination-owned selections", () => {
  const selection = {
    view: "chats",
    nodeId: "shared",
    chatId: "old-chat",
    episodeId: "old-episode",
  };
  const destination = {
    views: new Set(["chats"]),
    nodeIds: new Set(["shared"]),
    chatIds: new Set(),
    episodeIds: new Set(),
  };
  assert.deepEqual(selectionAfterGraphSwitch(selection, destination), {
    ...selection,
    chatId: null,
    episodeId: null,
  });
  assert.deepEqual(
    selectionAfterGraphSwitch(selection, {
      ...destination,
      nodeIds: new Set(),
      chatIds: new Set(["old-chat"]),
      episodeIds: new Set(["old-episode"]),
    }),
    { ...selection, nodeId: null },
  );
  assert.deepEqual(selectionAfterGraphSwitch(selection, { ...destination, views: new Set() }), {
    ...selection,
    view: "overview",
    chatId: null,
    episodeId: null,
  });
});

test("forks nest under their visible parent and retain exact provenance through archive filtering", () => {
  const make = (id, parent, extra = {}) => ({
    kind: "branch",
    branch_id: id,
    name: id,
    created_at: "2026-01-01T00:00:00Z",
    archived: false,
    running: false,
    parent_head: { target: parent, revision: 7 },
    ...extra,
  });
  const root = make("root", main);
  const fork = make("fork", { kind: "branch", branch_id: "root" }, { running: true });
  const nested = make("nested", { kind: "branch", branch_id: "fork" });
  const hidden = make("hidden", main, { archived: true });
  const refs = [nested, hidden, fork, root];
  assert.deepEqual(
    graphPickerOptions(refs, main).map((r) => r.branch_id),
    ["root", "fork", "nested"],
  );
  assert.equal(graphRefDepth(nested, refs), 2);
  assert.deepEqual(graphPickerOptions(refs, main)[1].parent_head, {
    target: { kind: "branch", branch_id: "root" },
    revision: 7,
  });
  assert.deepEqual(
    graphPickerOptions([nested, { ...fork, archived: true }, root], main).map((r) => r.branch_id),
    ["nested", "root"],
  );
  assert.equal(graphRefParentLabel({ ...root, parent_head: null }, refs), null);
});

test("only an open, eligible fork offers Merge; episode branches use episode Merge", () => {
  const fork = {
    kind: "branch",
    branch_id: "f",
    episode_id: null,
    archived: false,
    merge_eligible: true,
  };
  assert.equal(graphRefCanMerge(fork), true);
  assert.equal(graphRefCanMerge({ ...fork, episode_id: "e" }), true);
  assert.equal(graphRefCanMerge({ ...fork, branch_id: "e", episode_id: "e" }), false);
  assert.equal(graphRefCanMerge({ ...fork, archived: true }), false);
  assert.equal(graphRefCanMerge({ ...fork, merge_eligible: false }), false);
  assert.equal(graphRefCanMerge({ kind: "main", merge_eligible: false }), false);
});
