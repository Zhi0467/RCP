import assert from "node:assert/strict";
import test from "node:test";

import {
  createIdentityGate,
  createVoiceExecutor,
  voiceCallOutcome,
  voiceCommentary,
  voiceWatchFromResult,
} from "../src/voice/voiceExecutor.ts";
import { openVoiceSession } from "../src/voice/voiceSession.ts";

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
  failWith = null,
  unavailable = false,
} = {}) {
  const runs = [];
  const asked = [];
  const gate = createIdentityGate();
  let pinCalls = 0;
  const executor = createVoiceExecutor({
    gate,
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
            return { content: [{ type: "text", text: JSON.stringify({ ok: true }) }] };
          },
        },
      };
    },
    confirmMode: () => mode,
    pin: async (name, args) => {
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
  const declined = harness({ confirmations: [false] });
  assert.equal(
    code(await declined.executor.run(call("c1", "rcp_start_experiment", { experiment_id: "e" }))),
    "not_confirmed",
  );
  assert.equal(declined.runs.length, 0);

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
