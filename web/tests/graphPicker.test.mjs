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
const { graphPickerOptions, selectionAfterGraphSwitch } = await server.ssrLoadModule(
  "/src/graph/graphPickerModel.ts",
);
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
  const hash = projectViewHash("p", branch, "execution", { episodeId: "episode /+" });
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

test("picker puts main first, retains branch order, and keeps an archived active ref", () => {
  const a = { kind: "branch", branch_id: "a", archived: true };
  const b = { kind: "branch", branch_id: "b", archived: false };
  const c = { kind: "branch", branch_id: "c", archived: true };
  const m = { kind: "main", archived: false };
  const refs = [a, b, m, c];
  assert.deepEqual(graphPickerOptions(refs, main), [m, b]);
  assert.deepEqual(graphPickerOptions(refs, { kind: "branch", branch_id: "c" }), [m, b, c]);
  assert.deepEqual(graphPickerOptions(refs, main, true), [m, a, b, c]);
  assert.deepEqual(refs, [a, b, m, c]);
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
