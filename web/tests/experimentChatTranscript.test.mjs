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

const { visibleChatTranscriptTarget, transcriptAbsenceIsExpected, experimentChatFreshnessToken } =
  await server.ssrLoadModule("/src/hooks/useChatState.ts");
const { ApiError } = await server.ssrLoadModule("/src/api.ts");
const { MAIN_GRAPH } = await server.ssrLoadModule("/src/graphTarget.ts");

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
