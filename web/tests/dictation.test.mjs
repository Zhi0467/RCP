import assert from "node:assert/strict";
import test from "node:test";

import {
  chooseRecordingFormat,
  liveDictationSpan,
  modelChoices,
  voiceConnectionUpdate,
} from "../src/voice/dictation.ts";
import { MicrophoneBusyError, claimMicrophone } from "../src/voice/microphone.ts";

test("recording uses the first service format the browser can record, in service order", () => {
  const formats = ["audio/webm;codecs=opus", "audio/mp4;codecs=mp4a.40.2"];
  assert.equal(
    chooseRecordingFormat(formats, () => true),
    formats[0],
  );
  assert.equal(
    chooseRecordingFormat(formats, (type) => type === formats[1]),
    formats[1],
  );
  assert.equal(
    chooseRecordingFormat(formats, () => false),
    null,
  );
});

test("a transcription that returns after typing is dropped; a live one keeps its span", () => {
  const span = { sessionId: "a", start: 7, end: 7 };
  // Typing cleared the span, or a newer session took it over.
  assert.equal(liveDictationSpan(null, "a"), null);
  assert.equal(liveDictationSpan({ ...span, sessionId: "b" }, "a"), null);
  assert.equal(liveDictationSpan(span, "a"), span);
});

test("the microphone refuses a second holder until the first releases it", () => {
  const dictation = claimMicrophone("network_dictation");
  assert.throws(() => claimMicrophone("voice"), MicrophoneBusyError);
  dictation.release();

  const voice = claimMicrophone("voice");
  // A stale release from the earlier holder does not free the current one.
  dictation.release();
  assert.throws(() => claimMicrophone("native_dictation"), MicrophoneBusyError);
  voice.release();
});

test("the voice choice adds voice to the chosen connection and Off removes it from the holder", () => {
  const connections = [
    { id: "a", purposes: ["transcription", "voice"] },
    { id: "b", purposes: ["transcription"] },
  ];
  assert.deepEqual(voiceConnectionUpdate(connections, "b"), {
    id: "b",
    purposes: ["transcription", "voice"],
  });
  assert.deepEqual(voiceConnectionUpdate(connections, "off"), {
    id: "a",
    purposes: ["transcription"],
  });
  assert.equal(voiceConnectionUpdate(connections, "a"), null);
  assert.equal(voiceConnectionUpdate([connections[1]], "off"), null);
});

test("a model field keeps an id the provider list lacks, without duplicating listed ones", () => {
  const listed = ["gpt-transcribe", "whisper-1"];
  assert.deepEqual(modelChoices(listed, "whisper-1"), listed);
  assert.deepEqual(modelChoices(listed, "my-model"), ["my-model", ...listed]);
  assert.deepEqual(modelChoices(listed, ""), listed);
});
