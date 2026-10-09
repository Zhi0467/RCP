import assert from "node:assert/strict";
import test from "node:test";

import {
  NetworkDictationSession,
  joinDictatedText,
  transcribeInOrder,
} from "../src/voice/networkDictation.ts";

/** A recorder whose stop delivers its one chunk, like MediaRecorder, a tick later. */
function fakeRecorders() {
  const made = [];
  const createRecorder = () => {
    const recorder = {
      state: "inactive",
      label: `segment-${made.length + 1}`,
      ondataavailable: null,
      onstop: null,
      onerror: null,
      start() {
        this.state = "recording";
      },
      stop() {
        this.state = "inactive";
        queueMicrotask(() => {
          this.ondataavailable?.({ data: new Blob([this.label]) });
          this.onstop?.();
        });
      },
    };
    made.push(recorder);
    return recorder;
  };
  return { made, createRecorder };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function session(transcribe) {
  const { made, createRecorder } = fakeRecorders();
  const events = [];
  const hooks = {
    mimeType: "audio/webm",
    createRecorder,
    transcribe,
    onText: (text) => events.push(["text", text]),
    onRecordingStopped: () => events.push(["stopped"]),
    onSettled: () => events.push(["settled"]),
    onFailure: (error, pending) => events.push(["failed", error.message, pending.length]),
  };
  return { dictation: new NetworkDictationSession(hooks), made, events };
}

test("rolled-over segments transcribe in recording order and settle after finish", async () => {
  let release;
  const slowFirst = new Promise((resolve) => (release = resolve));
  const { dictation, made, events } = session(async (audio) => {
    const label = await audio.text();
    if (label === "segment-1") await slowFirst;
    return label;
  });
  dictation.start();
  dictation.rollover();
  dictation.finish();
  await settle();
  assert.equal(made.length, 2);
  assert.deepEqual(events, [["stopped"]]);
  release();
  await settle();
  assert.deepEqual(events, [
    ["stopped"],
    ["text", "segment-1"],
    ["text", "segment-2"],
    ["settled"],
  ]);
});

test("a failed segment stops recording and keeps it and every later segment", async () => {
  const { dictation, made, events } = session(async (audio) => {
    const label = await audio.text();
    if (label === "segment-1") throw new Error("denied");
    return label;
  });
  dictation.start();
  dictation.rollover();
  await settle();
  await settle();
  assert.equal(made[1].state, "inactive");
  assert.deepEqual(events, [["stopped"], ["failed", "denied", 2]]);
});

test("cancel drops results still in flight", async () => {
  const { dictation, events } = session(async (audio) => audio.text());
  dictation.start();
  dictation.cancel();
  await settle();
  assert.deepEqual(events, [["stopped"]]);
});

test("a retry returns the unfinished tail from the failed segment on", async () => {
  const texts = [];
  const segments = ["a", "b", "c"].map((part) => new Blob([part]));
  const failure = await transcribeInOrder(
    segments,
    async (audio) => {
      const text = await audio.text();
      if (text === "b") throw new Error("busy");
      return text;
    },
    (text) => texts.push(text),
  );
  assert.deepEqual(texts, ["a"]);
  assert.deepEqual(failure.pending, segments.slice(1));
});

test("segment text joins with one space and drops blank results", () => {
  assert.equal(joinDictatedText("", " first "), "first");
  assert.equal(joinDictatedText("first", "second"), " second");
  assert.equal(joinDictatedText("first ", "second"), "second");
  assert.equal(joinDictatedText("first", "  "), "");
});
