import assert from "node:assert/strict";
import test from "node:test";

import {
  artifactUrl,
  chatMessageTranscriptLine,
  chatTasksMissingFromHistory,
  latestNativeSessionId,
  projectActivityTask,
  reconstructTaskTranscript,
  relatedChatTasks,
  resumablePausedChatTask,
} from "../src/agentTasks.ts";

function task(overrides) {
  return {
    operation_id: overrides.operation_id,
    project_id: "project",
    kind: "node_chat",
    status: "succeeded",
    request: {},
    created_at: "2026-07-28T00:00:00Z",
    updated_at: "2026-07-28T00:00:00Z",
    status_message: "Done",
    attempt: 1,
    estimate_seconds: 1,
    estimate_samples: 0,
    phase: "done",
    elapsed_seconds: 1,
    progress: 1,
    can_pause: false,
    can_resume: false,
    can_retry: false,
    ...overrides,
  };
}

test("node chat reconstruction follows the latest chat id for that node", () => {
  const tasks = [
    task({
      operation_id: "old",
      request: { node_id: "node/a", chat_id: "old-chat", message: "Old question" },
    }),
    task({
      operation_id: "first",
      created_at: "2026-07-28T00:01:00Z",
      request: { node_id: "node/a", chat_id: "new-chat", message: "What does this mean?" },
      result: { messages: ["First answer"] },
      native_session_id: "native-1",
    }),
    task({
      operation_id: "other",
      created_at: "2026-07-28T00:02:00Z",
      request: { node_id: "node/b", chat_id: "other-chat", message: "Different node" },
    }),
    task({
      operation_id: "followup",
      created_at: "2026-07-28T00:03:00Z",
      request: { node_id: "node/a", chat_id: "new-chat", message: "Clarify it" },
      result: { messages: ["Clearer answer"] },
      native_session_id: "native-1",
    }),
  ];

  const related = relatedChatTasks(tasks, "node_chat", "node/a");
  assert.deepEqual(
    related.map((item) => item.operation_id),
    ["first", "followup"],
  );
  assert.deepEqual(
    reconstructTaskTranscript(related).map(({ role, text }) => ({ role, text })),
    [
      { role: "human", text: "What does this mean?" },
      { role: "agent", text: "First answer" },
      { role: "human", text: "Clarify it" },
      { role: "agent", text: "Clearer answer" },
    ],
  );
  assert.equal(latestNativeSessionId(related), "native-1");
});

test("node chat reconstruction can select an older explicit chat id", () => {
  const tasks = [
    task({
      operation_id: "old",
      request: { node_id: "node/a", chat_id: "old-chat", message: "Old question" },
    }),
    task({
      operation_id: "new",
      created_at: "2026-07-28T00:01:00Z",
      request: { node_id: "node/a", chat_id: "new-chat", message: "New question" },
    }),
  ];
  assert.deepEqual(
    relatedChatTasks(tasks, "node_chat", "node/a", "old-chat").map((item) => item.operation_id),
    ["old"],
  );
});

test("paused resumable attempts block a new chat turn", () => {
  const paused = task({ operation_id: "paused", status: "paused", can_resume: true });
  assert.equal(resumablePausedChatTask([paused])?.operation_id, "paused");
  assert.equal(resumablePausedChatTask([{ ...paused, can_resume: false }]), null);
});

test("a newer identical prompt is not hidden by older durable history", () => {
  const older = task({
    operation_id: "older",
    created_at: "2026-07-28T00:00:00Z",
    request: { message: "Same prompt" },
  });
  const active = task({
    operation_id: "active",
    status: "running",
    created_at: "2026-07-28T01:00:00Z",
    request: { message: "Same prompt" },
  });
  const messages = [
    {
      message_id: "message",
      role: "user",
      text: "Same prompt",
      timestamp: "2026-07-28T00:00:01Z",
      native_session_id: null,
      provider: null,
      model: null,
      reasoning: null,
      execution_machine: null,
      applied_revision: null,
    },
  ];
  assert.deepEqual(
    chatTasksMissingFromHistory([older, active], messages).map((item) => item.operation_id),
    ["active"],
  );
});

test("an assistant-only repair receipt suppresses reconstruction of its child task", () => {
  const repair = task({
    operation_id: "repair",
    parent_operation_id: "original",
    request: { message: "Original work", mode: "work" },
  });
  const messages = [
    {
      message_id: "receipt",
      operation_id: "repair",
      role: "assistant",
      text: "",
      timestamp: "2026-07-28T00:01:00Z",
      mode: "work",
      graph_update: null,
    },
  ];
  assert.deepEqual(chatTasksMissingFromHistory([repair], messages), []);
});

test("failed tasks preserve the human prompt and surfaced error", () => {
  const failed = task({
    operation_id: "failed",
    status: "failed",
    request: { node_id: "node/a", chat_id: "chat", message: "Rewrite this" },
    result: null,
    error: "Provider exited",
  });

  assert.deepEqual(
    reconstructTaskTranscript([failed]).map(({ role, text }) => ({ role, text })),
    [
      { role: "human", text: "Rewrite this" },
      { role: "error", text: "Provider exited" },
    ],
  );
});

test("failed chat tasks render a preserved answer before the error", () => {
  const failed = task({
    operation_id: "rejected-change",
    status: "failed",
    request: {
      node_id: "node/a",
      chat_id: "chat",
      message: "Explain this and update the graph",
    },
    result: { messages: ["Here is the explanation that completed before the edit failed."] },
    error: "The graph moved while this patch was being written",
  });

  assert.deepEqual(
    reconstructTaskTranscript([failed]).map(({ role, text }) => ({ role, text })),
    [
      { role: "human", text: "Explain this and update the graph" },
      { role: "agent", text: "Here is the explanation that completed before the edit failed." },
      { role: "error", text: "The graph moved while this patch was being written" },
    ],
  );
});

test("artifacts stay attached to the answer when a later task error is present", () => {
  const artifacts = [{ artifact_id: "a".repeat(24), name: "chart.html", media_type: "text/html" }];
  const failed = task({
    operation_id: "artifact-change-rejected",
    status: "failed",
    request: { node_id: "node/a", chat_id: "chat", message: "Show it and update the graph" },
    result: { messages: ["The result is **ready**."], artifacts },
    error: "Graph change rejected",
  });

  const transcript = reconstructTaskTranscript([failed]);
  assert.deepEqual(
    transcript.map(({ role, text }) => ({ role, text })),
    [
      { role: "human", text: "Show it and update the graph" },
      { role: "agent", text: "The result is **ready**." },
      { role: "error", text: "Graph change rejected" },
    ],
  );
  assert.deepEqual(transcript[1].artifacts, artifacts);
});

test("conversation reconstruction preserves immutable mode and graph receipt metadata", () => {
  const graphUpdate = {
    status: "applied",
    applied_revision: 12,
    change_summary: ["Recorded attempt exp/demo/attempt-2"],
    proposal_ids: ["proposal/decision"],
    validation_messages: [],
    correction_rounds: 1,
    repairable: false,
  };
  const completed = task({
    operation_id: "work-turn",
    request: { chat_id: "chat", message: "Run it", mode: "work" },
    result: { messages: ["The experiment completed."], graph_update: graphUpdate },
  });

  const transcript = reconstructTaskTranscript([completed]);
  assert.equal(transcript[0].mode, "work");
  assert.equal(transcript[1].mode, "work");
  assert.deepEqual(transcript[1].graphUpdate, graphUpdate);
});

test("legacy turns remain unlabelled and graph-only rejection is not an operational error", () => {
  const completed = task({
    operation_id: "rejected-reflection",
    request: { chat_id: "chat", message: "Run it" },
    status: "succeeded",
    error: "Graph update rejected",
    result: {
      messages: ["The experiment completed."],
      graph_update: {
        status: "rejected",
        applied_revision: null,
        change_summary: [],
        proposal_ids: [],
        validation_messages: ["The graph moved."],
        correction_rounds: 2,
        repairable: true,
      },
    },
  });

  const transcript = reconstructTaskTranscript([completed]);
  assert.equal(transcript[0].mode, null);
  assert.deepEqual(
    transcript.map(({ role, text }) => ({ role, text })),
    [
      { role: "human", text: "Run it" },
      { role: "agent", text: "The experiment completed." },
    ],
  );
  assert.equal(transcript[1].graphUpdate.status, "rejected");
});

test("durable chat lines retain their task identity with a legacy message fallback", () => {
  const record = {
    message_id: "message-id",
    operation_id: "task-id",
    role: "assistant",
    text: "Done",
    mode: "work",
    graph_update: null,
  };
  assert.equal(chatMessageTranscriptLine(record).taskId, "task-id");
  assert.equal(chatMessageTranscriptLine({ ...record, operation_id: null }).taskId, "message-id");
});

test("a watcher wake reconstructs only an attributed agent line", () => {
  const wake = task({
    operation_id: "watcher-wake",
    request: {
      chat_id: "chat",
      trigger: "watcher",
      message: "Inspect watcher/one and watcher/two",
      mode: "work",
    },
    result: { messages: ["Both detached jobs are finished."] },
  });
  const transcript = reconstructTaskTranscript([wake]);
  assert.deepEqual(
    transcript.map(({ role, text, trigger }) => ({ role, text, trigger })),
    [
      {
        role: "agent",
        text: "Both detached jobs are finished.",
        trigger: "watcher",
      },
    ],
  );
});

test("durable watcher messages never occupy the human side of a conversation", () => {
  const line = chatMessageTranscriptLine({
    message_id: "watcher-message",
    operation_id: "watcher-task",
    role: "assistant",
    text: "The watched work is gone.",
    mode: "work",
    graph_update: null,
    trigger: "watcher",
  });
  assert.equal(line.role, "agent");
  assert.equal(line.trigger, "watcher");
});

test("artifact URLs contain only RCP identifiers and the explicit action", () => {
  assert.equal(
    artifactUrl("project/id", "task id", "artifact#id", "preview"),
    "/api/projects/project%2Fid/tasks/task%20id/artifacts/artifact%23id/preview",
  );
  assert.equal(
    artifactUrl("project/id", "task id", "artifact#id", "download"),
    "/api/projects/project%2Fid/tasks/task%20id/artifacts/artifact%23id/download",
  );
});

test("project entry does not promote terminal task history into the activity strip", () => {
  const failed = task({ operation_id: "failed", status: "failed", error: "Provider exited" });
  const succeeded = task({ operation_id: "succeeded" });

  assert.equal(projectActivityTask([failed, succeeded], null), null);
});

test("project entry does not promote a paused attempt that already has a child", () => {
  const oldestPaused = task({ operation_id: "oldest-paused", status: "paused" });
  const pausedAncestor = task({
    operation_id: "paused-ancestor",
    status: "paused",
    parent_operation_id: "oldest-paused",
  });
  const failedChild = task({
    operation_id: "failed-child",
    status: "failed",
    parent_operation_id: "paused-ancestor",
    error: "Provider exited",
  });
  const laterRefresh = task({ operation_id: "later-refresh", kind: "refresh" });

  assert.equal(
    projectActivityTask([laterRefresh, failedChild, pausedAncestor, oldestPaused], null),
    null,
  );
});

test("project activity follows ongoing work and keeps its observed terminal result", () => {
  const running = task({ operation_id: "running", status: "running" });
  const paused = task({ operation_id: "paused", status: "paused" });
  const failed = task({ operation_id: "running", status: "failed", error: "Provider exited" });

  assert.equal(projectActivityTask([running], null)?.operation_id, "running");
  assert.equal(projectActivityTask([paused], null)?.operation_id, "paused");
  assert.equal(projectActivityTask([failed], "running")?.status, "failed");
});
