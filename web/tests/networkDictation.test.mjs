import assert from "node:assert/strict";
import test from "node:test";

import {
  FIRST_PIECE_MIN_MS,
  NetworkDictationSession,
  PAUSE_MS,
  addKeptSpeech,
  keptSpeech,
  setKeptSpeech,
  PIECE_MAX_MS,
  PIECE_MIN_MS,
  joinDictatedText,
  resolveKeptPieces,
  shouldStartNextPiece,
} from "../src/voice/networkDictation.ts";

/** Recorders whose stop delivers one chunk named for the piece, a tick later. */
function fakeRecorders() {
  const made = [];
  const createRecorder = () => {
    const recorder = {
      state: "inactive",
      label: `piece-${made.length + 1}`,
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

const settle = async () => {
  for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve));
};

function session(transcribe, isTransient = () => false) {
  const { made, createRecorder } = fakeRecorders();
  const events = [];
  const dictation = new NetworkDictationSession({
    mimeType: "audio/webm",
    createRecorder,
    transcribe,
    isTransient,
    wait: async () => {},
    onText: (text) => events.push(["text", text]),
    onBusy: () => {},
    onRecordingStopped: () => events.push(["stopped"]),
    onSettled: () => events.push(["settled"]),
    onKept: (pieces, error) =>
      events.push(["kept", pieces.map((piece) => piece.text ?? "audio"), error?.message ?? null]),
  });
  return { dictation, made, events };
}

const label = (audio) => audio.text();

test("pieces end at a pause after their minimum, or at the upload cap", () => {
  assert.equal(shouldStartNextPiece(FIRST_PIECE_MIN_MS - 1, PAUSE_MS, true), false);
  assert.equal(shouldStartNextPiece(FIRST_PIECE_MIN_MS, PAUSE_MS, true), true);
  assert.equal(shouldStartNextPiece(FIRST_PIECE_MIN_MS, PAUSE_MS, false), false);
  assert.equal(shouldStartNextPiece(PIECE_MIN_MS, PAUSE_MS - 1, false), false);
  assert.equal(shouldStartNextPiece(PIECE_MIN_MS, PAUSE_MS, false), true);
  assert.equal(shouldStartNextPiece(PIECE_MAX_MS, 0, false), true);
});

test("pieces reach the draft in recording order, then settle", async () => {
  let release;
  const slowFirst = new Promise((resolve) => (release = resolve));
  const { dictation, made, events } = session(async (audio) => {
    const text = await label(audio);
    if (text === "piece-1") await slowFirst;
    return text;
  });
  dictation.start(0);
  dictation.tick(FIRST_PIECE_MIN_MS, PAUSE_MS);
  dictation.finish();
  await settle();
  assert.equal(made.length, 2);
  release();
  await settle();
  assert.deepEqual(events, [["stopped"], ["text", "piece-1"], ["text", "piece-2"], ["settled"]]);
});

test("a transient failure retries once; a second failure keeps that piece and later ones", async () => {
  const attempts = new Map();
  const { dictation, events } = session(
    async (audio) => {
      const text = await label(audio);
      attempts.set(text, (attempts.get(text) ?? 0) + 1);
      if (text === "piece-2" || (text === "piece-1" && attempts.get(text) === 1))
        throw new Error("upstream");
      return text;
    },
    () => true,
  );
  dictation.start(0);
  dictation.tick(FIRST_PIECE_MIN_MS, PAUSE_MS);
  dictation.tick(FIRST_PIECE_MIN_MS + PIECE_MAX_MS, 0);
  await settle();
  assert.deepEqual(Object.fromEntries(attempts), { "piece-1": 2, "piece-2": 2 });
  assert.deepEqual(events, [
    ["text", "piece-1"],
    ["stopped"],
    ["kept", ["audio", "audio"], "upstream"],
  ]);
});

test("a refusal is not retried", async () => {
  let calls = 0;
  const { dictation, events } = session(async () => {
    calls += 1;
    throw new Error("denied");
  });
  dictation.start(0);
  dictation.finish();
  await settle();
  assert.equal(calls, 1);
  assert.deepEqual(events.at(-1), ["kept", ["audio"], "denied"]);
});

test("detaching keeps later text instead of writing it", async () => {
  const { dictation, events } = session(label);
  dictation.start(0);
  dictation.detach();
  await settle();
  assert.deepEqual(events, [["stopped"], ["kept", ["piece-1"], null]]);
});

test("cancel drops results still in flight", async () => {
  const { dictation, events } = session(label);
  dictation.start(0);
  dictation.cancel();
  await settle();
  assert.deepEqual(events, [["stopped"]]);
});

test("kept pieces resolve in order and return the tail after a failure", async () => {
  const pieces = [{ text: "one" }, { audio: new Blob(["two"]) }, { audio: new Blob(["bad"]) }];
  const result = await resolveKeptPieces(pieces, async (audio) => {
    const text = await audio.text();
    if (text === "bad") throw new Error("busy");
    return text;
  });
  assert.equal(result.text, "one two");
  assert.deepEqual(result.failure.pending, pieces.slice(2));
});

test("piece text joins with one space and drops blank results", () => {
  assert.equal(joinDictatedText("", " first "), "first");
  assert.equal(joinDictatedText("first", "second"), " second");
  assert.equal(joinDictatedText("first ", "second"), "second");
  assert.equal(joinDictatedText("first", "  "), "");
});

test("a next piece that cannot start leaves the current one recording", async () => {
  const { dictation, made, events } = session(label);
  dictation.start(0);
  const create = made[0];
  // Swap in a factory whose recorder throws on start.
  dictation.hooks.createRecorder = () => ({
    ...create,
    start() {
      throw new Error("busy");
    },
  });
  dictation.tick(FIRST_PIECE_MIN_MS, PAUSE_MS);
  assert.equal(made[0].state, "recording");
  dictation.finish();
  await settle();
  assert.deepEqual(events, [["stopped"], ["text", "piece-1"], ["settled"]]);
});

test("speech kept by overlapping sessions is appended, never replaced", () => {
  setKeptSpeech("chat", { pieces: [{ text: "first" }], error: null, resume: { draft: "", at: 0 } });
  addKeptSpeech("chat", { pieces: [{ text: "second" }], error: new Error("x"), resume: null });
  const kept = keptSpeech("chat");
  assert.deepEqual(kept.pieces, [{ text: "first" }, { text: "second" }]);
  assert.equal(kept.resume, null);
  setKeptSpeech("chat", null);
});
