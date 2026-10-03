/**
 * The one owner of the microphone in this page.
 *
 * Native dictation, network dictation, and a later voice session each claim it
 * before they listen; a second claim is refused while one is held. The native
 * recognizer opens the device itself, so its claim only reserves the owner.
 */
export type MicrophoneHolder = "native_dictation" | "network_dictation" | "voice";

export interface MicrophoneClaim {
  readonly holder: MicrophoneHolder;
  /** Open audio input for this claim; its tracks end when the claim is released. */
  open(): Promise<MediaStream>;
  release(): void;
}

export class MicrophoneBusyError extends Error {
  readonly holder: MicrophoneHolder;

  constructor(holder: MicrophoneHolder) {
    super("The microphone is already in use in RCP.");
    this.name = "MicrophoneBusyError";
    this.holder = holder;
  }
}

let current: MicrophoneClaim | null = null;

export function claimMicrophone(holder: MicrophoneHolder): MicrophoneClaim {
  if (current) throw new MicrophoneBusyError(current.holder);
  let stream: MediaStream | null = null;
  let released = false;
  const claim: MicrophoneClaim = {
    holder,
    async open() {
      if (released) throw new Error("The microphone was released.");
      if (stream) return stream;
      if (!globalThis.navigator?.mediaDevices?.getUserMedia)
        throw new Error("This browser cannot record audio here.");
      const opened = await navigator.mediaDevices.getUserMedia({ audio: true });
      // A release while permission was pending must not leave the device open.
      if (released) {
        stopTracks(opened);
        throw new Error("The microphone was released.");
      }
      stream = opened;
      return opened;
    },
    release() {
      if (released) return;
      released = true;
      if (stream) stopTracks(stream);
      stream = null;
      if (current === claim) current = null;
    },
  };
  current = claim;
  return claim;
}

function stopTracks(stream: MediaStream): void {
  for (const track of stream.getTracks()) track.stop();
}
