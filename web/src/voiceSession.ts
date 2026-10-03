// One GPT-Live voice session: microphone, WebRTC peer, and the `oai-events` data channel.
// Audio goes between this page and OpenAI; RCP's backend only exchanges the SDP.
// The page owns the lifetime: every way the session ends goes through `end`.

import { createVoiceSession } from "./api.ts";
import { claimMicrophone, type MicrophoneClaim } from "./microphone.ts";
import type { VoiceFunctionCall, VoiceIdentityGate } from "./voiceExecutor.ts";
import type { VoiceLimits, VoiceSessionResponse } from "./types";

/** How long `end` waits for `session.closed` before dropping the transport. */
const VOICE_CLOSE_WAIT_MS = 2_000;

export type VoiceEndReason =
  "member" | "idle" | "hard_cap" | "hidden" | "identity" | "space" | "connection" | "upstream";

export type VoiceSessionEvents = {
  onTranscript: (role: "member" | "agent", delta: string) => void;
  onFunctionCall: (call: VoiceFunctionCall) => void;
  onEnded: (reason: VoiceEndReason) => void;
};

export type VoiceSessionDeps = {
  claim?: () => MicrophoneClaim;
  createPeer?: () => RTCPeerConnection;
  requestSession?: (body: { sdp_offer: string; tools: unknown[] }) => Promise<VoiceSessionResponse>;
  playRemote?: (stream: MediaStream) => () => void;
};

export type VoiceSession = {
  limits: VoiceLimits;
  sendFunctionOutput: (callId: string, output: string) => void;
  /** A session-wide spoken update; `delegation_id: null` ties it to no delegation. */
  speak: (text: string) => void;
  /** Member activity that keeps the idle limit from firing. */
  noteActivity: () => void;
  /** Immediate drops the transport at once, for a page that is going away. */
  end: (reason: VoiceEndReason, options?: { immediate?: boolean }) => Promise<void>;
};

function playRemoteAudio(stream: MediaStream): () => void {
  const audio = new Audio();
  audio.autoplay = true;
  audio.srcObject = stream;
  void audio.play().catch(() => {});
  return () => {
    audio.pause();
    audio.srcObject = null;
  };
}

function eventId(): string {
  return `rcp_${globalThis.crypto.randomUUID()}`;
}

/** The finished function call inside a Responses delegation envelope, if this is one. */
export function delegatedFunctionCall(event: unknown): VoiceFunctionCall | null {
  const envelope = event as { type?: unknown; event?: { type?: unknown; item?: unknown } };
  if (envelope?.type !== "response.event") return null;
  if (envelope.event?.type !== "response.output_item.done") return null;
  const item = envelope.event.item as Record<string, unknown> | undefined;
  if (!item || (item.type !== undefined && item.type !== "function_call")) return null;
  const { call_id: callId, name, arguments: args } = item;
  if (typeof callId !== "string" || typeof name !== "string") return null;
  return { call_id: callId, name, arguments: typeof args === "string" ? args : "{}" };
}

/** Identity loss, even while the offer is in flight, ends the session at once. */
export async function openVoiceSession(
  tools: unknown[],
  events: VoiceSessionEvents,
  gate: VoiceIdentityGate,
  deps: VoiceSessionDeps = {},
): Promise<VoiceSession> {
  const claim = (deps.claim ?? (() => claimMicrophone("voice")))();
  let peer: RTCPeerConnection | null = null;
  const audio: { stop: (() => void) | null } = { stop: null };
  try {
    const stream = await claim.open();
    peer = (deps.createPeer ?? (() => new RTCPeerConnection()))();
    const pc = peer;
    for (const track of stream.getTracks()) pc.addTrack(track, stream);
    pc.ontrack = (event) => {
      audio.stop?.();
      audio.stop = (deps.playRemote ?? playRemoteAudio)(event.streams[0]);
    };
    const channel = pc.createDataChannel("oai-events");
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const answer = await (deps.requestSession ?? createVoiceSession)({
      sdp_offer: offer.sdp ?? "",
      tools,
    });
    await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp_answer });
    const session = startSession(answer.limits, pc, channel, claim, () => audio.stop?.(), events);
    gate.onLost(() => void session.end("identity", { immediate: true }));
    return session;
  } catch (error) {
    audio.stop?.();
    peer?.close();
    claim.release();
    throw error;
  }
}

function startSession(
  limits: VoiceLimits,
  pc: RTCPeerConnection,
  channel: RTCDataChannel,
  claim: MicrophoneClaim,
  stopAudio: () => void,
  events: VoiceSessionEvents,
): VoiceSession {
  let ending: Promise<void> | null = null;
  let markClosed: () => void = () => {};
  const closed = new Promise<void>((resolve) => {
    markClosed = resolve;
  });
  let idleTimer: ReturnType<typeof setTimeout> | null = null;
  const hardCapTimer = setTimeout(() => void end("hard_cap"), limits.hard_cap_seconds * 1000);

  const send = (event: Record<string, unknown>) => {
    if (channel.readyState === "open") channel.send(JSON.stringify(event));
  };
  const noteActivity = () => {
    if (ending) return;
    if (idleTimer !== null) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => void end("idle"), limits.idle_seconds * 1000);
  };

  let finished = false;
  const end = (reason: VoiceEndReason, options: { immediate?: boolean } = {}): Promise<void> => {
    const finish = () => {
      if (finished) return;
      finished = true;
      channel.close();
      pc.close();
      stopAudio();
      claim.release();
      events.onEnded(reason);
    };
    // A page going away cannot wait out a close already in progress.
    if (ending) {
      if (options.immediate) finish();
      return ending;
    }
    clearTimeout(hardCapTimer);
    if (idleTimer !== null) clearTimeout(idleTimer);
    const waitForClose = channel.readyState === "open" && !options.immediate;
    send({ type: "session.close", event_id: eventId() });
    ending = waitForClose
      ? Promise.race([
          closed,
          new Promise((resolve) => setTimeout(resolve, VOICE_CLOSE_WAIT_MS)),
        ]).then(finish)
      : Promise.resolve();
    if (!waitForClose) finish();
    return ending;
  };

  channel.onmessage = (message) => {
    let event: { type?: unknown; delta?: unknown };
    try {
      event = JSON.parse(String(message.data));
    } catch {
      return;
    }
    if (event.type === "session.input_transcript.delta" && typeof event.delta === "string") {
      noteActivity();
      events.onTranscript("member", event.delta);
    } else if (
      event.type === "session.output_transcript.delta" &&
      typeof event.delta === "string"
    ) {
      events.onTranscript("agent", event.delta);
    } else if (event.type === "session.closed") {
      markClosed();
      void end("upstream", { immediate: true });
    } else if (event.type === "error") {
      console.warn("Voice session event error.", event);
    } else {
      const call = delegatedFunctionCall(event);
      if (call) {
        noteActivity();
        events.onFunctionCall(call);
      }
    }
  };
  channel.onclose = () => {
    markClosed();
    void end("connection", { immediate: true });
  };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "failed" || pc.connectionState === "closed") {
      void end("connection", { immediate: true });
    }
  };
  noteActivity();

  return {
    limits,
    sendFunctionOutput: (callId, output) => {
      send({
        type: "response.item.create",
        event_id: eventId(),
        item: { type: "function_call_output", call_id: callId, output },
      });
      send({ type: "response.create", event_id: eventId() });
    },
    speak: (text) =>
      send({
        type: "session.commentary.append",
        event_id: eventId(),
        delegation_id: null,
        content: text.slice(0, limits.commentary_max_chars),
      }),
    noteActivity,
    end,
  };
}

/** A frozen page runs no timers, so the session ends as soon as the page hides or goes. */
export function endOnPageSuspend(end: () => void): () => void {
  const onVisibility = () => {
    if (document.visibilityState === "hidden") end();
  };
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("freeze", end);
  window.addEventListener("pagehide", end);
  return () => {
    document.removeEventListener("visibilitychange", onVisibility);
    document.removeEventListener("freeze", end);
    window.removeEventListener("pagehide", end);
  };
}
