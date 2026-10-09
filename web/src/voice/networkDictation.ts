/**
 * One network dictation session. It records back-to-back segments on one
 * microphone stream, so no upload outgrows the service's limits, and
 * transcribes them strictly in order. After a failure it keeps every
 * untranscribed segment in memory for a retry; nothing is ever written to disk.
 */

/** The slice of `MediaRecorder` a session drives; tests pass a fake. */
export interface DictationRecorder {
  readonly state: "inactive" | "recording" | "paused";
  start(): void;
  stop(): void;
  ondataavailable: ((event: BlobEvent) => void) | null;
  onstop: ((event: Event) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
}

export interface NetworkDictationHooks {
  mimeType: string;
  createRecorder: () => DictationRecorder;
  transcribe: (audio: Blob) => Promise<string>;
  /** One segment's text, in recording order. */
  onText: (text: string) => void;
  /** The last recorder stopped; the microphone may be released. */
  onRecordingStopped: () => void;
  /** Every segment was transcribed after `finish()`. */
  onSettled: () => void;
  /** A segment failed; `pending` holds it and every later segment, in order. */
  onFailure: (error: unknown, pending: Blob[]) => void;
}

export class NetworkDictationSession {
  private recorder: DictationRecorder | null = null;
  private queue: Promise<void> = Promise.resolve();
  private failure: { error: unknown; pending: Blob[] } | null = null;
  private cancelled = false;
  private readonly hooks: NetworkDictationHooks;

  constructor(hooks: NetworkDictationHooks) {
    this.hooks = hooks;
  }

  get recording(): boolean {
    return this.recorder?.state === "recording";
  }

  start(): void {
    this.record();
  }

  /** Close the current segment and keep recording into the next one. */
  rollover(): void {
    if (this.recording) this.record();
  }

  /** Stop recording; the remaining segments still transcribe. */
  finish(): void {
    if (this.recording) this.recorder?.stop();
  }

  /** Drop everything, including results still in flight. */
  cancel(): void {
    this.cancelled = true;
    if (this.recording) this.recorder?.stop();
  }

  private record(): void {
    const previous = this.recorder;
    const recorder = this.hooks.createRecorder();
    const chunks: Blob[] = [];
    recorder.ondataavailable = (event) => {
      if (event.data.size) chunks.push(event.data);
    };
    recorder.onerror = () => {
      this.failure ??= { error: new Error("Recording stopped unexpectedly."), pending: [] };
      if (recorder.state === "recording") recorder.stop();
    };
    recorder.onstop = () => {
      if (!this.cancelled) this.enqueue(new Blob(chunks, { type: this.hooks.mimeType }));
      if (recorder !== this.recorder) return;
      this.recorder = null;
      this.hooks.onRecordingStopped();
      void this.queue.then(() => {
        if (this.cancelled) return;
        if (this.failure) this.hooks.onFailure(this.failure.error, this.failure.pending);
        else this.hooks.onSettled();
      });
    };
    // The next segment starts before the last one stops, so no speech falls between.
    this.recorder = recorder;
    recorder.start();
    if (previous?.state === "recording") previous.stop();
  }

  private enqueue(audio: Blob): void {
    if (!audio.size) return;
    this.queue = this.queue.then(async () => {
      if (this.cancelled) return;
      if (this.failure) {
        this.failure.pending.push(audio);
        return;
      }
      try {
        const text = await this.hooks.transcribe(audio);
        if (!this.cancelled) this.hooks.onText(text);
      } catch (error) {
        this.failure = { error, pending: [audio] };
        // Later speech would fail the same way; stop and keep it for a retry.
        this.finish();
      }
    });
  }
}

/** Transcribe kept segments in order; returns the unfinished tail on failure. */
export async function transcribeInOrder(
  segments: Blob[],
  transcribe: (audio: Blob) => Promise<string>,
  onText: (text: string) => void,
): Promise<{ error: unknown; pending: Blob[] } | null> {
  for (let index = 0; index < segments.length; index += 1) {
    try {
      onText(await transcribe(segments[index]));
    } catch (error) {
      return { error, pending: segments.slice(index) };
    }
  }
  return null;
}

/** Join a segment's text to what dictation already wrote, with one space between. */
export function joinDictatedText(before: string, text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  return before && !/\s$/.test(before) ? ` ${trimmed}` : trimmed;
}
