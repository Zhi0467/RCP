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

const { visibleChatTranscriptTarget, transcriptAbsenceIsExpected } = await server.ssrLoadModule(
  "/src/hooks/useChatState.ts",
);
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
