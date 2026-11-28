import assert from "node:assert/strict";
import test from "node:test";

import {
  COMPACT_OFFER_IDLE_MS,
  COMPACT_OFFER_MIN_TOKENS,
  compactOffer,
  compactionUnsent,
} from "../src/chat/compactOffer.ts";

const ENDED = "2026-11-27T09:00:00Z";
const ended = Date.parse(ENDED);

function task(overrides = {}) {
  return {
    operation_id: "turn-1",
    active: false,
    finished: true,
    failed: false,
    native_session_id: "session-1",
    runtime_id: "claude.stream-json.v1",
    parent_operation_id: null,
    can_compact_first: true,
    finished_at: ENDED,
    updated_at: ENDED,
    request: { provider: "claude", model: null, session_id: "session-1" },
    agent_status: {
      context_tokens: COMPACT_OFFER_MIN_TOKENS,
      context_window: 1_000_000,
      autocompact_tokens: null,
      compacting: false,
      compactions: [],
      subagents: [],
      messages: [],
    },
    ...overrides,
  };
}

function offer({ tasks = [task()], messages = [], sessionId = "session-1", idle = 0 } = {}) {
  return compactOffer({
    tasks,
    statusTasks: tasks,
    messages,
    sessionId,
    now: ended + COMPACT_OFFER_IDLE_MS + idle,
  });
}

test("a large session idle past the caches is offered a compaction", () => {
  assert.deepEqual(offer(), {
    contextTokens: COMPACT_OFFER_MIN_TOKENS,
    idleMs: COMPACT_OFFER_IDLE_MS,
  });
});

test("no offer before the idle hour, below the token floor, or while a turn runs", () => {
  assert.equal(offer({ idle: -1 }), null);
  const small = task({
    agent_status: { ...task().agent_status, context_tokens: COMPACT_OFFER_MIN_TOKENS - 1 },
  });
  assert.equal(offer({ tasks: [small] }), null);
  assert.equal(offer({ tasks: [task(), task({ operation_id: "turn-2", finished: false })] }), null);
});

test("any later turn or message resets the idle clock, so the next message is not asked", () => {
  const later = new Date(ended + 60_000).toISOString();
  assert.equal(offer({ messages: [{ timestamp: later }] }), null);
  assert.equal(offer({ tasks: [task({ finished_at: later })] }), null);
});

test("no offer without a resumed session or on a runtime that cannot compact", () => {
  assert.equal(offer({ sessionId: null }), null);
  assert.equal(offer({ tasks: [task({ can_compact_first: false })] }), null);
  assert.equal(offer({ sessionId: "another-session" }), null);
});

test("a compact-first turn that failed before compacting never sent its message", () => {
  const failed = task({ failed: true, request: { ...task().request, compact_first: true } });
  assert.equal(compactionUnsent(failed), true);
  const compacted = {
    ...failed,
    agent_status: { ...failed.agent_status, compactions: [{ trigger: "manual" }] },
  };
  assert.equal(compactionUnsent(compacted), false);
  assert.equal(compactionUnsent(task({ failed: true })), false);
});
