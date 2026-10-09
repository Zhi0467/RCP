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
const {
  loadChatSummaryPage,
  loadChatInventory,
  chatSummariesForTarget,
  loadChatTranscript,
  mergeChatSummaryPage,
  nextChatSummaryOffset,
  reconcileChatSelectionAfterRefresh,
} = await server.ssrLoadModule("/src/chat/chatApi.ts");

test("chat summary loading fetches exactly the requested page", async () => {
  const calls = [];
  const items = Array.from({ length: 205 }, (_, index) => ({
    chat_id: `chat-${index}`,
    kind: "project_chat",
    node_id: null,
    title: `Chat ${index}`,
    updated_at: "2026-07-31T00:00:00Z",
    message_count: 2,
    last_message_preview: "Answer",
  }));
  const result = await loadChatSummaryPage("/api/projects/project", 200, async (path) => {
    calls.push(path);
    const offset = Number(new URL(`http://rcp${path}`).searchParams.get("offset"));
    return { items: items.slice(offset, offset + 200), total: items.length, offset, limit: 200 };
  });
  assert.equal(result.items.length, 5);
  assert.deepEqual(calls, ["/api/projects/project/chats?offset=200&limit=200"]);
});

test("chat pages append without duplicating ids", () => {
  const existing = [
    { chat_id: "a", title: "A" },
    { chat_id: "b", title: "B" },
  ];
  const next = [
    { chat_id: "b", title: "B stale" },
    { chat_id: "c", title: "C" },
  ];
  assert.deepEqual(
    mergeChatSummaryPage(existing, next, "append").map(({ chat_id, title }) => ({
      chat_id,
      title,
    })),
    [
      { chat_id: "a", title: "A" },
      { chat_id: "b", title: "B" },
      { chat_id: "c", title: "C" },
    ],
  );
});

test("refreshing page zero replaces loaded pages and resets the pagination offset", () => {
  const existing = [
    { chat_id: "a", title: "A stale" },
    { chat_id: "b", title: "B" },
    { chat_id: "c", title: "C" },
  ];
  const refreshed = [
    { chat_id: "new", title: "New" },
    { chat_id: "a", title: "A fresh" },
  ];
  assert.deepEqual(
    mergeChatSummaryPage(existing, refreshed, "refresh").map(({ chat_id, title }) => ({
      chat_id,
      title,
    })),
    [
      { chat_id: "new", title: "New" },
      { chat_id: "a", title: "A fresh" },
    ],
  );
  assert.equal(nextChatSummaryOffset({ items: refreshed, total: 4, offset: 0, limit: 2 }), 2);
});

test("a selected transcript deleted from canonical storage is cleared on refresh", () => {
  const previous = { chat_id: "deleted", title: "Deleted" };
  assert.deepEqual(
    reconcileChatSelectionAfterRefresh("deleted", previous, [{ chat_id: "current" }], null),
    { selectedChatId: null, retainedSummary: null, deleteTranscript: true },
  );
});

test("a selected chat outside refreshed page zero is retained only after exact validation", () => {
  const previous = { chat_id: "selected", title: "Old title" };
  const transcript = {
    chat_id: "selected",
    title: "Current title",
    messages: [
      {
        message_id: "message",
        role: "assistant",
        text: "Current",
        timestamp: "2026-08-01T00:00:00Z",
      },
    ],
  };
  assert.deepEqual(
    reconcileChatSelectionAfterRefresh(
      "selected",
      previous,
      [{ chat_id: "first-page" }],
      transcript,
    ),
    { selectedChatId: "selected", retainedSummary: transcript, deleteTranscript: false },
  );
});

test("load more resumes from the refreshed cursor and preserves a valid selection", async () => {
  const firstPage = [
    { chat_id: "new", title: "New" },
    { chat_id: "selected", title: "Selected" },
  ];
  const refreshed = mergeChatSummaryPage(
    [{ chat_id: "stale", title: "Stale" }, ...firstPage],
    firstPage,
    "refresh",
  );
  const offset = nextChatSummaryOffset({ items: firstPage, total: 3, offset: 0, limit: 2 });
  const page = await loadChatSummaryPage("/api/projects/project", offset, async (path) => {
    assert.equal(path, "/api/projects/project/chats?offset=2&limit=200");
    return { items: [{ chat_id: "last", title: "Last" }], total: 3, offset: 2, limit: 200 };
  });
  const loaded = mergeChatSummaryPage(refreshed, page.items, "append");

  assert.deepEqual(
    loaded.map((item) => item.chat_id),
    ["new", "selected", "last"],
  );
  assert.deepEqual(
    reconcileChatSelectionAfterRefresh("selected", firstPage[1], firstPage, undefined),
    { selectedChatId: "selected", retainedSummary: null, deleteTranscript: false },
  );
  assert.equal(nextChatSummaryOffset(page), 3);
});

test("inventory loads one complete snapshot before exact-target selection excludes other graphs", async () => {
  const branch = { kind: "branch", branch_id: "branch-1" };
  const main = { kind: "main" };
  const summaries = Array.from({ length: 201 }, (_, index) => ({
    chat_id: `chat-${index}`,
    graph_target: index === 200 ? branch : main,
    message_count: index === 200 ? 0 : 2,
  }));
  const calls = [];
  const inventory = await loadChatInventory("/api/projects/project", async (path) => {
    const params = new URL(path, "http://rcp").searchParams;
    assert.equal(params.get("inventory"), "true");
    assert.equal(params.has("branch_id"), false);
    calls.push(path);
    return { items: summaries, offset: 0, total: 201, limit: 200 };
  });
  assert.equal(calls.length, 1);
  assert.equal(inventory.length, 201);
  assert.deepEqual(
    chatSummariesForTarget(inventory, branch).map((item) => item.chat_id),
    ["chat-200"],
  );
  assert.equal(chatSummariesForTarget(inventory, main).length, 200);
  await assert.rejects(
    loadChatInventory("/api/projects/project", async () => ({
      items: summaries.slice(0, 200),
      offset: 0,
      total: 201,
      limit: 200,
    })),
  );
});

test("transcript validation refuses a branch transcript when viewing main", async () => {
  await assert.rejects(
    loadChatTranscript("/api/projects/project", "chat", async () => ({
      chat_id: "chat",
      graph_target: { kind: "branch", branch_id: "branch-1" },
      messages: [],
    })),
  );
});
