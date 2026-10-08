import assert from "node:assert/strict";
import test from "node:test";

import {
  appendVoiceTranscript,
  boundVoiceTranscript,
  createVoiceSourceLabels,
  createVoiceSaveQueue,
  createFinishedResultOffer,
  voiceWatchFromReceipt,
  createIdentityGate,
  createVoiceExecutor,
  voiceCallOutcome,
  voiceCommentary,
  voiceWatchFromResult,
} from "../src/voice/voiceExecutor.ts";
import { endOnPageSuspend, openVoiceSession } from "../src/voice/voiceSession.ts";

const TOOLS = [
  { name: "rcp_get_project_overview", confirm: () => false, readOnly: true },
  { name: "rcp_send_conversation_message", confirm: (args) => args.mode === "work" },
  { name: "rcp_start_experiment", confirm: () => true },
  { name: "rcp_run_terminal_command", confirm: () => true, alwaysConfirm: true },
];

function harness({
  mode = "tap",
  confirmations = [],
  pins = null,
  pinError = null,
  failWith = null,
  unavailable = false,
  receiptDeps = {},
  result = { ok: true },
} = {}) {
  const runs = [];
  const asked = [];
  const gate = createIdentityGate();
  let pinCalls = 0;
  const executor = createVoiceExecutor({
    gate,
    ...receiptDeps,
    catalog: () =>
      TOOLS.map(({ name, confirm, alwaysConfirm }) => ({ name, confirm, alwaysConfirm })),
    resolve: (name) => {
      if (unavailable) return { ok: false, refusal: "not now" };
      const tool = TOOLS.find((candidate) => candidate.name === name);
      return {
        ok: true,
        definition: {
          name,
          annotations: { readOnlyHint: Boolean(tool?.readOnly) },
          execute: async (args) => {
            runs.push({ name, args });
            if (failWith) throw failWith;
            return { content: [{ type: "text", text: JSON.stringify(result) }] };
          },
        },
      };
    },
    confirmMode: () => mode,
    pin: async (name, args) => {
      if (pinError) throw pinError;
      const budget = pins ? pins[pinCalls] : 4;
      pinCalls += 1;
      return {
        tool: name,
        project_id: "p",
        arguments: { ...args, invocation_ceiling: budget },
        budget,
      };
    },
    requestConfirmation: async (pin) => {
      asked.push(pin);
      return confirmations.shift() ?? false;
    },
  });
  return { executor, runs, asked, gate };
}

const call = (call_id, name, args = {}) => ({ call_id, name, arguments: JSON.stringify(args) });
const code = (output) => JSON.parse(output).code;

test("a name outside the catalog never runs", async () => {
  const { executor, runs } = harness();
  assert.equal(code(await executor.run(call("c1", "rcp_delete_project"))), "unknown_tool");
  assert.equal(runs.length, 0);
});

test("tap mode runs a confirm call only after Confirm, with the pinned arguments", async () => {
  const receipts = [];
  const declined = harness({
    confirmations: [false],
    receiptDeps: {
      target: () => receiptTarget,
      saveReceipt: async (receipt) => receipts.push(receipt),
    },
  });
  assert.equal(
    code(await declined.executor.run(call("c1", "rcp_start_experiment", { experiment_id: "e" }))),
    "not_confirmed",
  );
  assert.equal(declined.runs.length, 0);
  assert.equal(receipts.length, 0);

  const confirmed = harness({ confirmations: [true] });
  await confirmed.executor.run(call("c1", "rcp_start_experiment", { experiment_id: "e" }));
  assert.equal(confirmed.asked.length, 1);
  assert.deepEqual(confirmed.runs, [
    { name: "rcp_start_experiment", args: { experiment_id: "e", invocation_ceiling: 4 } },
  ]);
});

test("a tool the page cannot run is refused before any card", async () => {
  const { executor, asked } = harness({ unavailable: true, confirmations: [true] });
  assert.equal(code(await executor.run(call("c1", "rcp_start_experiment"))), "refused");
  assert.equal(asked.length, 0);
});

test("Confirm refuses when a pinned value changed", async () => {
  const { executor, runs } = harness({ confirmations: [true], pins: [4, 9] });
  assert.equal(
    code(await executor.run(call("c1", "rcp_start_experiment", { experiment_id: "e" }))),
    "changed",
  );
  assert.equal(runs.length, 0);
});

test("without confirming, a confirm call runs at once", async () => {
  const { executor, runs, asked } = harness({ mode: "none" });
  await executor.run(call("c1", "rcp_send_conversation_message", { message: "m", mode: "work" }));
  assert.equal(asked.length, 0);
  assert.equal(runs.length, 1);
});

test("an always-confirm tool shows its card even without confirming", async () => {
  const { executor, runs, asked } = harness({ mode: "none", confirmations: [true] });
  await executor.run(call("c1", "rcp_run_terminal_command", { repository_id: "r", command: "ls" }));
  assert.equal(asked.length, 1);
  assert.equal(runs.length, 1);
});

test("in tap mode, a call its tool does not confirm runs at once", async () => {
  const { executor, runs, asked } = harness();
  await executor.run(
    call("c1", "rcp_send_conversation_message", { message: "m", mode: "discuss" }),
  );
  assert.equal(asked.length, 0);
  assert.equal(runs.length, 1);
});

test("a repeated call_id or an identical unknown-outcome repeat does not run again", async () => {
  const { executor, runs } = harness({ mode: "none", failWith: new TypeError("dropped") });
  const args = { message: "m", mode: "work" };
  assert.equal(
    code(await executor.run(call("c1", "rcp_send_conversation_message", args))),
    "unknown_outcome",
  );
  assert.equal(await executor.run(call("c1", "rcp_send_conversation_message", args)), null);
  assert.equal(
    code(await executor.run(call("c2", "rcp_send_conversation_message", args))),
    "unknown_outcome",
  );
  assert.equal(runs.length, 1);
});

function fakeTransport() {
  const sent = [];
  const state = { peerClosed: false, released: false, offers: [] };
  const channel = {
    readyState: "open",
    send: (text) => sent.push(JSON.parse(text)),
    close() {
      this.readyState = "closed";
    },
  };
  const deps = {
    claim: () => ({
      holder: "voice",
      open: async () => ({ getTracks: () => [] }),
      release: () => {
        state.released = true;
      },
    }),
    createPeer: () => ({
      iceGatheringState: "new",
      localDescription: null,
      listeners: {},
      addTrack() {},
      createDataChannel: () => channel,
      createOffer: async () => ({ type: "offer", sdp: "offer" }),
      async setLocalDescription(description) {
        this.localDescription = description;
        this.iceGatheringState = "gathering";
        setTimeout(() => {
          this.localDescription = { type: "offer", sdp: "offer with candidates" };
          this.iceGatheringState = "complete";
          this.listeners.icegatheringstatechange?.();
        });
      },
      addEventListener(type, listener) {
        this.listeners[type] = listener;
      },
      removeEventListener(type) {
        delete this.listeners[type];
      },
      setRemoteDescription: async () => {},
      close: () => {
        state.peerClosed = true;
      },
    }),
    requestSession: async (body) => {
      state.offers.push(body.sdp_offer);
      return {
        sdp_answer: "answer",
        limits: {
          idle_seconds: 60,
          hard_cap_seconds: 60,
          confirm_timeout_seconds: 5,
          commentary_max_chars: 200,
        },
      };
    },
    playRemote: () => () => {},
  };
  return { deps, sent, state };
}

test("identity loss ends the session and refuses the next call, including a cached read", async () => {
  const { executor, runs, gate } = harness();
  const transport = fakeTransport();
  const ended = [];
  await openVoiceSession(
    [],
    { onTranscript() {}, onFunctionCall() {}, onEnded: (reason) => ended.push(reason) },
    gate,
    transport.deps,
  );
  gate.lose();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(ended, ["identity"]);
  assert.ok(transport.sent.some((event) => event.type === "session.close"));
  assert.ok(transport.state.peerClosed && transport.state.released);
  assert.equal(code(await executor.run(call("c1", "rcp_get_project_overview"))), "identity");
  assert.equal(runs.length, 0);
});

test("the offer is sent only after ICE gathering, with its candidates", async () => {
  const transport = fakeTransport();
  const session = await openVoiceSession(
    [],
    { onTranscript() {}, onFunctionCall() {}, onEnded() {} },
    harness().gate,
    transport.deps,
  );
  assert.deepEqual(transport.state.offers, ["offer with candidates"]);
  await session.end("member", { immediate: true });
});

test("identity loss while connecting aborts the offer and frees the microphone", async () => {
  const { gate } = harness();
  const transport = fakeTransport();
  let signal;
  transport.deps.requestSession = (_body, given) => {
    signal = given;
    gate.lose();
    return Promise.resolve({ sdp_answer: "answer", limits: {} });
  };
  await assert.rejects(
    openVoiceSession(
      [],
      { onTranscript() {}, onFunctionCall() {}, onEnded() {} },
      gate,
      transport.deps,
    ),
  );
  assert.equal(signal.aborted, true);
  assert.ok(transport.state.released && transport.state.peerClosed);
});

test("after End, a late call never dispatches and the member's reason stands", async () => {
  const { gate } = harness();
  const transport = fakeTransport();
  const calls = [];
  const ended = [];
  const session = await openVoiceSession(
    [],
    {
      onTranscript() {},
      onFunctionCall: (item) => calls.push(item),
      onEnded: (reason) => ended.push(reason),
    },
    gate,
    transport.deps,
  );
  const channel = transport.deps.createPeer().createDataChannel();
  const deliver = (event) => channel.onmessage({ data: JSON.stringify(event) });
  const closing = session.end("member");
  deliver({
    type: "response.event",
    event: {
      type: "response.output_item.done",
      item: { type: "function_call", call_id: "late", name: "rcp_get_project_overview" },
    },
  });
  deliver({ type: "session.closed" });
  await closing;
  assert.equal(calls.length, 0);
  assert.deepEqual(ended, ["member"]);
});

test("a call outcome separates refusals from results", () => {
  assert.equal(
    voiceCallOutcome('{"ok":false,"code":"not_confirmed","error":"x"}').code,
    "not_confirmed",
  );
  assert.equal(voiceCallOutcome('{"project_id":"p"}').ok, true);
  assert.equal(voiceCallOutcome("plain text").ok, true);
});

test("a watch is named for the project its result reports, not the open one", () => {
  const names = new Map([["origin", "Origin"]]);
  const output = JSON.stringify({ project_id: "origin", episode_id: "e1" });
  const watch = voiceWatchFromResult(
    "rcp_authorize_auto_research",
    {},
    output,
    (id) => names.get(id) ?? "",
  );
  assert.equal(watch.project_id, "origin");
  assert.equal(watch.project_name, "Origin");
});

test("completion commentary depends only on kind, project name, and status", () => {
  const spoken = voiceCommentary("experiment", "Alpha", "finished", 200);
  assert.equal(voiceCommentary("experiment", "Alpha", "finished", 200), spoken);
  assert.notEqual(voiceCommentary("auto_research", "Alpha", "finished", 200), spoken);
  assert.notEqual(voiceCommentary("experiment", "Beta", "finished", 200), spoken);
  assert.notEqual(voiceCommentary("experiment", "Alpha", "needs_you", 200), spoken);
  assert.equal(voiceCommentary.length, 4);
  assert.ok(voiceCommentary("experiment", "A".repeat(500), "finished", 80).length <= 80);
});

test("an event past the idle deadline ends the session instead of extending it", async () => {
  const transport = fakeTransport();
  let time = 0;
  const heard = [];
  const ended = [];
  await openVoiceSession(
    [],
    {
      onTranscript: (_role, delta) => heard.push(delta),
      onFunctionCall() {},
      onEnded: (reason) => ended.push(reason),
    },
    harness().gate,
    {
      ...transport.deps,
      now: () => time,
      requestSession: async (body) => {
        const answer = await transport.deps.requestSession(body);
        return { ...answer, limits: { ...answer.limits, hard_cap_seconds: 600 } };
      },
    },
  );
  const channel = transport.deps.createPeer().createDataChannel();
  // A held-back timer never ran; speech arriving after the deadline must not reopen it.
  time = 60_000;
  channel.onmessage({
    data: JSON.stringify({ type: "session.input_transcript.delta", delta: "hi" }),
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(heard, []);
  assert.ok(transport.sent.some((event) => event.type === "session.close"));
  channel.onmessage({ data: JSON.stringify({ type: "session.closed" }) });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(ended, ["idle"]);
  assert.ok(transport.state.released);
});

test("hiding ends voice unless the window keeps running while hidden", () => {
  const saved = { document: globalThis.document, window: globalThis.window };
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: "hidden" });
  globalThis.window = new EventTarget();
  try {
    for (const keepWhileHidden of [false, true]) {
      const ends = [];
      const stop = endOnPageSuspend(() => ends.push("end"), { keepWhileHidden });
      document.dispatchEvent(new Event("visibilitychange"));
      assert.equal(ends.length, keepWhileHidden ? 0 : 1);
      // A frozen or departing page cannot enforce limits, so these always end it.
      document.dispatchEvent(new Event("freeze"));
      window.dispatchEvent(new Event("pagehide"));
      assert.equal(ends.length, keepWhileHidden ? 2 : 3);
      stop();
    }
  } finally {
    globalThis.document = saved.document;
    globalThis.window = saved.window;
  }
});

const receiptTarget = {
  project_id: "p",
  project_name: "Project",
  graph_target: { kind: "main", branch_id: null },
};

test("durable receipt precedes dispatch and restores a target-scoped unknown fence", async () => {
  const receipts = [];
  let first;
  first = harness({
    mode: "none",
    failWith: new TypeError("dropped"),
    receiptDeps: {
      target: () => receiptTarget,
      saveReceipt: async (receipt) => {
        assert.equal(first.runs.length, 0);
        receipts.push(receipt);
      },
    },
  });
  const args = { mode: "work", message: "m" };
  assert.equal(
    code(await first.executor.run(call("a", "rcp_send_conversation_message", args))),
    "unknown_outcome",
  );
  assert.equal(receipts[0].argument_fingerprint.length, 64);
  const resumed = harness({
    mode: "none",
    receiptDeps: { target: () => receiptTarget, initialReceipts: receipts },
  });
  assert.equal(
    code(await resumed.executor.run(call("b", "rcp_send_conversation_message", args))),
    "unknown_outcome",
  );
  assert.equal(resumed.runs.length, 0);
  const other = harness({
    mode: "none",
    receiptDeps: {
      target: () => ({ ...receiptTarget, project_id: "other" }),
      initialReceipts: receipts,
    },
  });
  await other.executor.run(call("b", "rcp_send_conversation_message", args));
  assert.equal(other.runs.length, 1);
});

test("accepted receipts restore exact watches but never confirmation authority", async () => {
  const receipts = [];
  const original = harness({
    confirmations: [true],
    result: { project_id: "p", episode_id: "e" },
    receiptDeps: {
      target: () => receiptTarget,
      saveReceipt: async (receipt) => receipts.push(receipt),
    },
  });
  await original.executor.run(call("a", "rcp_start_experiment"));
  assert.deepEqual(
    receipts.map((receipt) => receipt.outcome),
    ["unknown", "accepted"],
  );
  const accepted = receipts.at(-1);
  assert.equal(voiceWatchFromReceipt(accepted).id, "e");
  const resumed = harness({
    receiptDeps: { target: () => receiptTarget, initialReceipts: [accepted] },
  });
  assert.equal(
    code(await resumed.executor.run(call("b", "rcp_start_experiment"))),
    "not_confirmed",
  );
  assert.equal(resumed.runs.length, 0);
});

test("refusal receipts only replace a persisted pre-dispatch unknown receipt", async () => {
  for (const dispatched of [false, true]) {
    const receipts = [];
    const run = harness({
      confirmations: [true],
      pinError: dispatched ? null : new Error("cannot pin"),
      failWith: new Error("refused"),
      receiptDeps: {
        target: () => receiptTarget,
        saveReceipt: async (receipt) => receipts.push(receipt),
      },
    });
    assert.equal(code(await run.executor.run(call("a", "rcp_start_experiment"))), "refused");
    assert.deepEqual(
      receipts.map((receipt) => receipt.outcome),
      dispatched ? ["unknown", "refused"] : [],
    );
  }
});

test("a failed pending receipt save prevents side effects", async () => {
  const run = harness({
    mode: "none",
    receiptDeps: {
      target: () => receiptTarget,
      saveReceipt: async () => {
        throw new Error("save failed");
      },
    },
  });
  assert.equal(code(await run.executor.run(call("a", "rcp_start_experiment"))), "refused");
  assert.equal(run.runs.length, 0);
});

test("transcript bounds include metadata, receipts, complete unicode, and newest entries", () => {
  const raw = appendVoiceTranscript([], "member", "😀".repeat(100), "item-1");
  const record = { entries: raw, receipts: [], revision: 1, updated_at: 1, id: "session" };
  const limits = { entry_bytes: 16, session_bytes: 550, max_entries: 3 };
  const bounded = boundVoiceTranscript(record, limits);
  assert.ok(bounded.entries.length > 0 && bounded.entries.length <= 3);
  assert.ok(Buffer.byteLength(JSON.stringify(bounded)) <= limits.session_bytes);
  assert.ok(
    bounded.entries.every(
      (entry) => Buffer.byteLength(entry.text) <= 16 && !entry.text.includes("�"),
    ),
  );
  const newest = boundVoiceTranscript(
    { ...bounded, entries: appendVoiceTranscript(bounded.entries, "agent", "newest", "item-2") },
    limits,
  );
  assert.equal(newest.entries.at(-1).text, "newest");
  assert.equal(newest.entries.at(-1).provider_order, "item-2");
  assert.equal(
    appendVoiceTranscript(
      [{ speaker: "member", text: "first", provider_order: null, source: null }],
      "member",
      " next",
    )[0].text,
    "first next",
  );
  const quoted = appendVoiceTranscript([], "agent", "a".repeat(24), "item-3", "tool:read");
  const continued = appendVoiceTranscript(quoted, "agent", "b", "item-3", "tool:read");
  const fresh = appendVoiceTranscript(continued, "agent", "c", "item-3", null);
  assert.equal(fresh.length, 2);
  assert.equal(fresh[0].source, "tool:read");
  assert.equal(fresh[1].source, null);
  assert.throws(() =>
    boundVoiceTranscript({ ...record, receipts: [{ tool: "x".repeat(600) }] }, limits),
  );
});

test("source labels stay with their call and expire on the next response item", () => {
  const labels = createVoiceSourceLabels();
  labels.capture("a", "read", "{}", receiptTarget);
  labels.capture("b", "read", "{}", { ...receiptTarget, project_id: "other" });
  labels.discard("b");
  labels.succeeded("a");
  const source = labels.speech("agent", "item-1");
  const expected = createVoiceSourceLabels();
  expected.capture("a", "read", "{}", receiptTarget);
  expected.succeeded("a");
  assert.equal(source, expected.speech("agent", "item-1"));
  assert.notEqual(source, null);
  assert.equal(labels.speech("agent", "item-1"), source);
  assert.equal(labels.speech("agent", "item-2"), null);
  labels.capture("c", "read", "{}", receiptTarget);
  labels.succeeded("c");
  assert.equal(labels.speech("agent", "item-3"), source);
  assert.equal(labels.speech("member", "member-1"), null);
  assert.equal(labels.speech("agent", "item-4"), null);
});

test("two reads through one tool and target keep distinct sources", () => {
  const labels = createVoiceSourceLabels();
  const sourceOf = (callId, args, item) => {
    labels.capture(callId, "rcp_read", args, receiptTarget);
    labels.succeeded(callId);
    return labels.speech("agent", item);
  };
  assert.notEqual(
    sourceOf("a", '{"route":"/graph"}', "item-1"),
    sourceOf("b", '{"route":"/history"}', "item-2"),
  );
});

test("saves serialize, coalesce pending snapshots, and stop after identity changes", async () => {
  let release;
  let current = true;
  const saved = [];
  const queue = createVoiceSaveQueue(
    async (snapshot) => {
      saved.push(snapshot);
      if (snapshot === 1)
        await new Promise((resolve) => {
          release = resolve;
        });
    },
    () => current,
  );
  const first = queue.save(1);
  await new Promise((resolve) => setImmediate(resolve));
  const second = queue.save(2);
  const third = queue.save(3);
  assert.deepEqual(saved, [1]);
  release();
  await Promise.all([first, second, third]);
  await queue.flush();
  assert.deepEqual(saved, [1, 3]);
  current = false;
  await assert.rejects(queue.save(4));
  assert.deepEqual(saved, [1, 3]);
});

test("finished results only open on explicit acceptance under captured project and graph", async () => {
  let target = receiptTarget;
  let listed = 0;
  const opened = [];
  const offer = createFinishedResultOffer({
    currentTarget: () => target,
    list: async (watch) => {
      listed++;
      assert.equal(watch.id, "t");
      return [{ viewer_id: "v", can_open: true }];
    },
    open: async (id) => opened.push(id),
  });
  const watch = { id: "t", project_id: "p", kind: "work_turn", record: "task", last: "finished" };
  offer.offer(watch, target);
  assert.equal(listed, 0);
  assert.deepEqual(opened, []);
  target = { ...receiptTarget, graph_target: { kind: "branch", branch_id: "b" } };
  await assert.rejects(offer.open());
  assert.equal(listed, 0);
  target = receiptTarget;
  assert.equal(await offer.open(), 1);
  assert.deepEqual(opened, ["v"]);
  await assert.rejects(offer.open());
});

test("finished offers expire after their immediate reply or the next agent response", async () => {
  const opened = [];
  const watch = { id: "t", project_id: "p", kind: "work_turn", record: "task", last: "finished" };
  const offer = createFinishedResultOffer({
    currentTarget: () => receiptTarget,
    list: async () => [{ viewer_id: "v", can_open: true }],
    open: async (id) => opened.push(id),
  });
  offer.offer(watch, receiptTarget);
  offer.speech("member", "reply");
  offer.speech("member", "reply");
  assert.equal(await offer.open(), 1);
  offer.offer(watch, receiptTarget);
  offer.speech("member", "first");
  offer.speech("member", "later");
  await assert.rejects(offer.open());
  offer.offer(watch, receiptTarget);
  offer.speech("agent", "response");
  await assert.rejects(offer.open());
  assert.deepEqual(opened, ["v"]);
});

test("commentary queued during connection flushes once on open and is discarded after End", async () => {
  for (const endBeforeOpen of [false, true]) {
    const transport = fakeTransport();
    const channel = transport.deps.createPeer().createDataChannel();
    channel.readyState = "connecting";
    const session = await openVoiceSession(
      [],
      {
        onTranscript() {},
        onFunctionCall() {},
        onEnded() {},
      },
      harness().gate,
      transport.deps,
    );
    session.speak("queued");
    assert.equal(transport.sent.length, 0);
    if (endBeforeOpen) await session.end("member", { immediate: true });
    channel.readyState = "open";
    channel.onopen();
    channel.onopen();
    assert.equal(
      transport.sent.filter((event) => event.type === "session.commentary.append").length,
      endBeforeOpen ? 0 : 1,
    );
    await session.end("member", { immediate: true });
  }
});

test("output sent after a missed deadline ends the session instead of reaching OpenAI", async () => {
  const transport = fakeTransport();
  let time = 0;
  const session = await openVoiceSession(
    [],
    { onTranscript() {}, onFunctionCall() {}, onEnded() {} },
    harness().gate,
    { ...transport.deps, now: () => time },
  );
  time = 600_000;
  session.sendFunctionOutput("c1", "{}");
  session.speak("done");
  assert.ok(!transport.sent.some((event) => event.type === "response.create"));
  assert.ok(!transport.sent.some((event) => event.type === "session.commentary.append"));
  assert.ok(transport.sent.some((event) => event.type === "session.close"));
  await session.end("member", { immediate: true });
});
