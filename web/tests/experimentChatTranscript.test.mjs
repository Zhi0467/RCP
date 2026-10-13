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
  visibleChatTranscriptTarget,
  transcriptAbsenceIsExpected,
  experimentChatFreshnessToken,
  visibleUnreadChatId,
  shouldLoadVisibleChatTranscript,
} = await server.ssrLoadModule("/src/chat/useChatState.ts");
const { ApiError } = await server.ssrLoadModule("/src/core/api.ts");
const { MAIN_GRAPH } = await server.ssrLoadModule("/src/core/graphTarget.ts");

const BRANCH = { kind: "branch", branch_id: "branch-1" };

test("a branch-scoped Experiment chat loads against its own graph, not the viewed one", () => {
  // The Runs panel href carries `branch`, while the viewed target comes from
  // `branch_id`, so the app still views main while the route is on a branch.
  assert.deepEqual(
    visibleChatTranscriptTarget("experiment-chat", "experiment-chat", BRANCH, MAIN_GRAPH),
    BRANCH,
  );
});

test("every other visible chat keeps the viewed graph", () => {
  assert.deepEqual(
    visibleChatTranscriptTarget("other-chat", "experiment-chat", BRANCH, MAIN_GRAPH),
    MAIN_GRAPH,
  );
  assert.deepEqual(
    visibleChatTranscriptTarget("other-chat", "experiment-chat", BRANCH, BRANCH),
    BRANCH,
  );
});

test("a branch orchestrator opened from main reads its own branch", () => {
  const summaries = [
    { chat_id: "orchestrator", graph_target: BRANCH, conversation_kind: "auto_research" },
    { chat_id: "branch-chat", graph_target: BRANCH, conversation_kind: "chat" },
  ];
  assert.deepEqual(
    visibleChatTranscriptTarget("orchestrator", null, MAIN_GRAPH, MAIN_GRAPH, summaries),
    BRANCH,
  );
  assert.deepEqual(
    visibleChatTranscriptTarget("branch-chat", null, MAIN_GRAPH, MAIN_GRAPH, summaries),
    MAIN_GRAPH,
  );
});

test("a chat selected while viewing a branch keeps that branch", () => {
  // Without an exact route the chat id comes from the viewed graph's own
  // project projection, so the viewed target is the correct one; defaulting to
  // main here would 404 every Experiment chat opened inside a branch view.
  assert.deepEqual(
    visibleChatTranscriptTarget("experiment-chat", "experiment-chat", BRANCH, BRANCH),
    BRANCH,
  );
});

test("a main-target Experiment chat keeps the viewed graph", () => {
  assert.deepEqual(
    visibleChatTranscriptTarget("experiment-chat", "experiment-chat", MAIN_GRAPH, MAIN_GRAPH),
    MAIN_GRAPH,
  );
});

test("an Experiment turn with no captured transcript is not an error", () => {
  // A run that has just started has no graph transcript until a Work turn is
  // captured, and the Runs panel asks for it without a summary to prove it
  // exists. That 404 must not reach the error banner.
  assert.equal(
    transcriptAbsenceIsExpected(
      "experiment-chat",
      "experiment-chat",
      new ApiError("Chat not found", 404),
    ),
    true,
  );
});

test("a missing chat the user selected elsewhere still reports", () => {
  assert.equal(
    transcriptAbsenceIsExpected(
      "other-chat",
      "experiment-chat",
      new ApiError("Chat not found", 404),
    ),
    false,
  );
});

test("a real failure on the Experiment chat still reports", () => {
  assert.equal(
    transcriptAbsenceIsExpected("experiment-chat", "experiment-chat", new ApiError("Boom", 500)),
    false,
  );
  assert.equal(
    transcriptAbsenceIsExpected(
      "experiment-chat",
      "experiment-chat",
      new Error("Conversation returned a different graph target."),
    ),
    false,
  );
});

const PROGRESS = {
  current_operation_id: "op-1",
  current_status: "running",
  current_last_activity_at: "2026-09-09T00:00:00Z",
};

test("a cross-graph Experiment chat takes its freshness from the run's progress", () => {
  // Summaries only cover the viewed graph, so a branch chat never supplies the
  // `updated_at` that refires the transcript fetch for every other chat.
  const token = experimentChatFreshnessToken("experiment-chat", PROGRESS, BRANCH, MAIN_GRAPH);
  assert.notEqual(token, "");
  assert.notEqual(
    token,
    experimentChatFreshnessToken(
      "experiment-chat",
      { ...PROGRESS, current_last_activity_at: "2026-09-09T00:00:05Z" },
      BRANCH,
      MAIN_GRAPH,
    ),
  );
  assert.notEqual(
    token,
    experimentChatFreshnessToken(
      "experiment-chat",
      { ...PROGRESS, current_operation_id: "op-2" },
      BRANCH,
      MAIN_GRAPH,
    ),
  );
  assert.equal(
    token,
    experimentChatFreshnessToken("experiment-chat", { ...PROGRESS }, BRANCH, MAIN_GRAPH),
  );
});

test("a chat on the viewed graph keeps summary freshness alone", () => {
  // Its summary `updated_at` already refires the fetch; a second signal would
  // only refetch the same transcript on every run poll.
  assert.equal(
    experimentChatFreshnessToken("experiment-chat", PROGRESS, MAIN_GRAPH, MAIN_GRAPH),
    "",
  );
  assert.equal(experimentChatFreshnessToken("experiment-chat", PROGRESS, BRANCH, BRANCH), "");
});

test("no selected Experiment chat needs no freshness signal", () => {
  assert.equal(experimentChatFreshnessToken(null, PROGRESS, BRANCH, MAIN_GRAPH), "");
});

test("a cross-graph chat with no progress yet still yields a stable token", () => {
  assert.equal(experimentChatFreshnessToken("experiment-chat", null, BRANCH, MAIN_GRAPH), "||");
});

test("the Agents board shows no transcript, so it marks no chat read", () => {
  assert.equal(visibleUnreadChatId("chats", "chat-1", null, true), null);
  assert.equal(visibleUnreadChatId("chats", "chat-1", null, false), "chat-1");
});

test("a task-only inventory row loads no transcript until messages exist", () => {
  for (const experimentId of [null, "task-chat"]) {
    assert.equal(
      shouldLoadVisibleChatTranscript(
        "task-chat",
        [{ chat_id: "task-chat", message_count: 0 }],
        experimentId,
      ),
      false,
    );
    assert.equal(
      shouldLoadVisibleChatTranscript(
        "task-chat",
        [{ chat_id: "task-chat", message_count: 1 }],
        experimentId,
      ),
      true,
    );
  }
});

test("transcript resources retain their owning ref even for equal chat ids", async () => {
  const { chatResourceKey } = await server.ssrLoadModule("/src/chat/chatResources.ts");
  const { resourceKeyId } = await server.ssrLoadModule("/src/core/resourceStore.ts");
  assert.notEqual(
    resourceKeyId(chatResourceKey("p", MAIN_GRAPH, "c")),
    resourceKeyId(chatResourceKey("p", BRANCH, "c")),
  );
  assert.equal(chatResourceKey("p", BRANCH, "c").ref, "branch:branch-1");
});

test("tab chat capture retains human UI without server payloads", async () => {
  const { cloneChatStateSnapshot } = await server.ssrLoadModule("/src/chat/useChatState.ts");
  const draft = { chatId: "draft", kind: "project", nodeId: null, title: "Unsent" };
  const captured = cloneChatStateSnapshot({
    floatingChat: null,
    selectedChatId: "draft",
    draftConversations: [draft],
    chatTranscripts: new Map([["old", {}]]),
  });
  assert.deepEqual(captured, {
    floatingChat: null,
    selectedChatId: "draft",
    draftConversations: [draft],
  });
});

test("transcript profile preserves missing responses for viewer-specific handling", async () => {
  const { chatResourceProfile } = await server.ssrLoadModule("/src/chat/chatResources.ts");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "Missing conversation" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  try {
    await assert.rejects(
      chatResourceProfile("p", MAIN_GRAPH, "regular", "/api/projects/p").fetch(
        new AbortController().signal,
      ),
      (error) => error instanceof ApiError && error.status === 404,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
