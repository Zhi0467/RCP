/**
 * Network dictation with no length limit. One microphone stream records
 * back-to-back pieces, each small enough for one upload, cut at a pause.
 * Pieces transcribe strictly in order. Speech that cannot reach the draft (a
 * failure, typing, leaving the chat) is kept in memory per chat, never on disk.
 */

import { useSyncExternalStore } from "react";

/** The slice of `MediaRecorder` a session drives; tests pass a fake. */
export interface DictationRecorder {
  readonly state: "inactive" | "recording" | "paused";
  start(): void;
  stop(): void;
  ondataavailable: ((event: BlobEvent) => void) | null;
  onstop: ((event: Event) => void) | null;
  onerror: ((event: ErrorEvent) => void) | null;
}

// The first piece is short so a failing service shows within seconds; later
// pieces end at the first pause past 40 s, and never run past one upload.
export const FIRST_PIECE_MIN_MS = 5_000;
export const PIECE_MIN_MS = 40_000;
export const PIECE_MAX_MS = 55_000;
export const PAUSE_MS = 400;
export const TRANSIENT_RETRY_MS = 2_000;

export function shouldStartNextPiece(pieceMs: number, quietMs: number, first: boolean): boolean {
  if (pieceMs >= PIECE_MAX_MS) return true;
  return pieceMs >= (first ? FIRST_PIECE_MIN_MS : PIECE_MIN_MS) && quietMs >= PAUSE_MS;
}

/** One kept piece: text that came back, or audio still to transcribe. */
/**
 * One kept piece: text that came back, or audio still to transcribe. `order` is
 * when the piece began recording, so speech kept by overlapping sessions stays
 * in recording order however their uploads finish.
 */
export type KeptPiece = ({ text: string } | { audio: Blob }) & { order: number };

export interface KeptSpeech {
  /** Each kept audio piece carries its recording's type. */
  pieces: KeptPiece[];
  /** Why the speech is kept: a failure, or null when typing or leaving kept it. */
  error: unknown;
  /** The draft and offset to continue at, while the draft is unchanged. */
  resume: { draft: string; at: number } | null;
}

export function keptSpeechNeedsService(kept: KeptSpeech): boolean {
  return kept.pieces.some((piece) => "audio" in piece);
}

export interface NetworkDictationHooks {
  mimeType: string;
  createRecorder: () => DictationRecorder;
  transcribe: (audio: Blob) => Promise<string>;
  /** A failure one retry may fix: a server error, timeout, or dropped connection. */
  isTransient: (error: unknown) => boolean;
  wait?: (ms: number) => Promise<void>;
  /** One piece's text, in recording order, while the session writes the draft. */
  onText: (text: string) => void;
  /** How many pieces are uploading or waiting to. */
  onBusy: (pieces: number) => void;
  /** The last recorder stopped; the microphone may be released. */
  onRecordingStopped: () => void;
  /** Every piece reached the draft. */
  onSettled: () => void;
  /** Some speech could not reach the draft; `error` is null when it was detached. */
  onKept: (pieces: KeptPiece[], error: unknown) => void;
}

export class NetworkDictationSession {
  private readonly hooks: NetworkDictationHooks;
  private recorder: DictationRecorder | null = null;
  private pieceStartedAt = 0;
  private firstPiece = true;
  private queue: Promise<void> = Promise.resolve();
  private busy = 0;
  private failure: unknown = null;
  private kept: KeptPiece[] | null = null;
  private cancelled = false;

  constructor(hooks: NetworkDictationHooks) {
    this.hooks = hooks;
  }

  get recording(): boolean {
    return this.recorder?.state === "recording";
  }

  start(now: number): void {
    this.record(now);
  }

  /** Called on a short interval with how long the microphone has been quiet. */
  tick(now: number, quietMs: number): void {
    if (!this.recording) return;
    if (!shouldStartNextPiece(now - this.pieceStartedAt, quietMs, this.firstPiece)) return;
    try {
      this.record(now);
      this.firstPiece = false;
    } catch {
      // At a pause the current piece keeps recording and the next tick tries
      // again; at the cap it must end, or it would outgrow one upload.
      if (now - this.pieceStartedAt >= PIECE_MAX_MS) this.finish();
    }
  }

  /** Stop recording; the remaining pieces still reach the draft. */
  finish(): void {
    if (this.recording) this.recorder?.stop();
  }

  /** Stop recording; every result from now on is kept instead of written. */
  detach(): void {
    this.kept ??= [];
    this.finish();
  }

  /** Drop everything, including results still in flight. */
  cancel(): void {
    this.cancelled = true;
    this.finish();
  }

  private record(now: number): void {
    const startedAt = now;
    const previous = this.recorder;
    const recorder = this.hooks.createRecorder();
    const chunks: Blob[] = [];
    recorder.ondataavailable = (event) => {
      if (event.data.size) chunks.push(event.data);
    };
    // A lost microphone ends recording like Stop: what was heard still counts.
    recorder.onerror = () => {
      if (recorder.state === "recording") recorder.stop();
    };
    recorder.onstop = () => {
      if (!this.cancelled) this.enqueue(new Blob(chunks, { type: this.hooks.mimeType }), startedAt);
      if (recorder !== this.recorder) return;
      this.recorder = null;
      this.hooks.onRecordingStopped();
      void this.queue.then(() => {
        if (this.cancelled) return;
        if (this.kept?.length) this.hooks.onKept(this.kept, this.failure);
        else this.hooks.onSettled();
      });
    };
    // The next piece starts before the last one stops, so no speech falls between.
    // A recorder that cannot start throws here and leaves the current one recording.
    recorder.start();
    this.recorder = recorder;
    this.pieceStartedAt = now;
    if (previous?.state === "recording") previous.stop();
  }

  private enqueue(audio: Blob, order: number): void {
    if (!audio.size) return;
    this.setBusy(1);
    this.queue = this.queue.then(async () => {
      try {
        if (this.cancelled) return;
        if (this.failure !== null) {
          this.kept?.push({ audio, order });
          return;
        }
        try {
          const text = await this.transcribe(audio);
          if (this.cancelled) return;
          if (this.kept) this.kept.push({ text, order });
          else this.hooks.onText(text);
        } catch (error) {
          this.failure = error;
          this.kept ??= [];
          this.kept.push({ audio, order });
          // Later speech would fail the same way; stop and keep it.
          this.finish();
        }
      } finally {
        this.setBusy(-1);
      }
    });
  }

  private async transcribe(audio: Blob): Promise<string> {
    try {
      return await this.hooks.transcribe(audio);
    } catch (error) {
      if (!this.hooks.isTransient(error)) throw error;
      const wait =
        this.hooks.wait ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
      await wait(TRANSIENT_RETRY_MS);
      return this.hooks.transcribe(audio);
    }
  }

  private setBusy(delta: number): void {
    this.busy += delta;
    this.hooks.onBusy(this.busy);
  }
}

/** Kept pieces as text, transcribing audio in order; the unfinished tail on failure. */
export async function resolveKeptPieces(
  pieces: KeptPiece[],
  transcribe: (audio: Blob) => Promise<string>,
): Promise<{ text: string; failure: { error: unknown; pending: KeptPiece[] } | null }> {
  let text = "";
  for (let index = 0; index < pieces.length; index += 1) {
    const piece = pieces[index];
    try {
      const next = "text" in piece ? piece.text : await transcribe(piece.audio);
      text += joinDictatedText(text, next);
    } catch (error) {
      return { text, failure: { error, pending: pieces.slice(index) } };
    }
  }
  return { text, failure: null };
}

/** Join a piece's text to what dictation already wrote, with one space between. */
export function joinDictatedText(before: string, text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  return before && !/\s$/.test(before) ? ` ${trimmed}` : trimmed;
}

/** Quiet time from the microphone level; zero when the browser cannot measure it. */
export function createQuietMeter(stream: MediaStream): {
  quietMs: (now: number) => number;
  close: () => void;
} {
  if (typeof AudioContext === "undefined") return { quietMs: () => 0, close: () => {} };
  const context = new AudioContext();
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  context.createMediaStreamSource(stream).connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  // Start from a quiet room, so speech heard first never becomes the floor.
  let floor = 0.003;
  let quietSince: number | null = null;
  return {
    quietMs(now) {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) sum += sample * sample;
      const level = Math.sqrt(sum / samples.length);
      // The room's noise floor rises slowly and falls at once, so speech stays above it.
      floor = Math.min(floor * 1.002 + 1e-5, level);
      if (level < Math.max(0.006, floor * 2)) quietSince ??= now;
      else quietSince = null;
      return quietSince === null ? 0 : now - quietSince;
    },
    close() {
      void context.close();
    },
  };
}

// Kept speech belongs to its chat and member while the app runs, never to disk.
// It shares the chat's member-scoped draft key; with no member there is none.
const keptByChat = new Map<string, KeptSpeech>();
const keptListeners = new Set<() => void>();
/** Add speech after what the chat already keeps, so overlapping sessions lose nothing. */
export function addKeptSpeech(chatKey: string | null, kept: KeptSpeech): void {
  if (!chatKey) return;
  const current = keptByChat.get(chatKey);
  setKeptSpeech(
    chatKey,
    current
      ? {
          pieces: [...current.pieces, ...kept.pieces].sort((a, b) => a.order - b.order),
          error: kept.error ?? current.error,
          resume: null,
        }
      : kept,
  );
}
export function setKeptSpeech(chatKey: string | null, kept: KeptSpeech | null): void {
  if (!chatKey) return;
  if (kept) keptByChat.set(chatKey, kept);
  else keptByChat.delete(chatKey);
  keptListeners.forEach((listener) => listener());
}
export function keptSpeech(chatKey: string | null): KeptSpeech | null {
  return chatKey ? (keptByChat.get(chatKey) ?? null) : null;
}
export function useKeptSpeech(chatKey: string | null): KeptSpeech | null {
  return useSyncExternalStore(
    (listener) => {
      keptListeners.add(listener);
      return () => {
        keptListeners.delete(listener);
      };
    },
    () => keptSpeech(chatKey),
    () => null,
  );
}
