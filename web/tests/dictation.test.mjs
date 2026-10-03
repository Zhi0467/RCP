import assert from "node:assert/strict";
import test from "node:test";

import { replaceTextSpan } from "../src/chatInput.ts";
import { chooseRecordingFormat, liveDictationSpan } from "../src/dictation.ts";
import { MicrophoneBusyError, claimMicrophone } from "../src/microphone.ts";

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

test("a transcription that returns after typing is dropped; a live one replaces its span", () => {
  const span = { sessionId: "a", start: 7, end: 7 };
  // Typing cleared the span, or a newer session took it over.
  assert.equal(liveDictationSpan(null, "a"), null);
  assert.equal(liveDictationSpan({ ...span, sessionId: "b" }, "a"), null);

  const live = liveDictationSpan(span, "a");
  assert.deepEqual(replaceTextSpan("before  after", live, "spoken words"), {
    value: "before spoken words after",
    end: 19,
  });
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
