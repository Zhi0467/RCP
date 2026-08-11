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
const {
  cacheProjectTabState,
  cachedSnapshotCanReplace,
  canonicalRevisionNeedsReload,
  canonicalRevisionPollDelay,
  loadCanonicalRevision,
  projectTabStateForOpen,
} = await server.ssrLoadModule("/src/App.tsx");

after(() => server.close());

test("canonical revision polling uses only the lightweight project endpoint", async () => {
  const requested = [];
  const revision = await loadCanonicalRevision(async (path) => {
    requested.push(path);
    return { revision: 12 };
  }, "/api/projects/project-1");

  assert.equal(revision, 12);
  assert.deepEqual(requested, ["/api/projects/project-1/cached/revision"]);
});

test("the per-project display cache is bounded and refreshed as an LRU", () => {
  const cache = new Map();
  cacheProjectTabState(cache, "alpha", { revision: 1 }, 2);
  cacheProjectTabState(cache, "beta", { revision: 2 }, 2);
  cacheProjectTabState(cache, "alpha", { revision: 3 }, 2);
  cacheProjectTabState(cache, "gamma", { revision: 4 }, 2);

  assert.deepEqual([...cache.keys()], ["alpha", "gamma"]);
  assert.deepEqual(cache.get("alpha"), { revision: 3 });
});

test("a cached response cannot move the rendered project backwards", () => {
  const snapshot = (id, revision) => ({ id, graph: { revision } });
  assert.equal(cachedSnapshotCanReplace("alpha", 8, snapshot("alpha", 7)), false);
  assert.equal(cachedSnapshotCanReplace("alpha", 8, snapshot("alpha", 8)), true);
  assert.equal(cachedSnapshotCanReplace("alpha", 8, snapshot("beta", 2)), true);
});

test("returning to a cached tab restores its complete render state without an empty loading frame", () => {
  const retained = {
    project: { id: "alpha", graph: { revision: 8 }, paper: { content: "draft" } },
    tasks: [{ operation_id: "task-1" }],
    watchers: [{ watcher_id: "watcher-1" }],
    chatSummaries: [{ chat_id: "chat-1" }],
    chatTranscripts: new Map([["chat-1", { chat_id: "chat-1", messages: [] }]]),
    historyRevisionSummaries: [{ to_revision: 8 }],
    viewState: {
      view: "chats",
      panelScroll: [["chats", 420]],
      researchSubview: "dag",
      dagViewport: { zoom: 1.2, scrollLeft: 10, scrollTop: 20 },
    },
  };
  const cache = new Map([
    ["alpha", retained],
    ["beta", { project: { id: "beta" } }],
  ]);

  const open = projectTabStateForOpen(cache, "alpha");
  assert.equal(open.loading, false);
  assert.strictEqual(open.state, retained);
  assert.equal(open.state.project.graph.revision, 8);
  assert.equal(open.state.tasks[0].operation_id, "task-1");
  assert.equal(open.state.watchers[0].watcher_id, "watcher-1");
  assert.equal(open.state.chatSummaries[0].chat_id, "chat-1");
  assert.equal(open.state.chatTranscripts.get("chat-1").chat_id, "chat-1");
  assert.equal(open.state.historyRevisionSummaries[0].to_revision, 8);
  assert.deepEqual(open.state.viewState.panelScroll, [["chats", 420]]);
  assert.deepEqual([...cache.keys()], ["beta", "alpha"]);
  assert.equal(projectTabStateForOpen(cache, "missing"), null);
});

test("canonical state reloads only after the accepted revision advances", () => {
  assert.equal(canonicalRevisionNeedsReload(8, 7), true);
  assert.equal(canonicalRevisionNeedsReload(7, 7), false);
  assert.equal(canonicalRevisionNeedsReload(6, 7), false);
});

test("canonical revision polling backs off after failures and stays bounded", () => {
  assert.equal(canonicalRevisionPollDelay(0), 2_000);
  assert.equal(canonicalRevisionPollDelay(1), 4_000);
  assert.equal(canonicalRevisionPollDelay(4), 30_000);
  assert.equal(canonicalRevisionPollDelay(20), 30_000);
});
