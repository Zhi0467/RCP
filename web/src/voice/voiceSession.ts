// One GPT-Live voice session: microphone, WebRTC peer, and the `oai-events` data channel.
// Audio goes between this page and OpenAI; RCP's backend only exchanges the SDP.
// The page owns the lifetime: every way the session ends goes through `end`.

import { createVoiceSession } from "../core/api.ts";
import { RCP_PLAYBOOK } from "../webmcp/playbook.ts";
import { claimMicrophone, type MicrophoneClaim } from "./microphone.ts";
import type { VoiceFunctionCall, VoiceIdentityGate } from "./voiceExecutor.ts";
import type { VoiceLimits, VoiceSessionResponse } from "../core/types";

/** How long `end` waits for `session.closed` before dropping the transport. */
const VOICE_CLOSE_WAIT_MS = 2_000;
/** How long setup waits for ICE gathering, as in OpenAI's WebRTC sequence. */
const VOICE_ICE_GATHER_WAIT_MS = 10_000;
/** How often the session checks its idle and hard-cap deadlines. */
const VOICE_DEADLINE_TICK_MS = 1_000;

export type VoiceEndReason =
  "member" | "idle" | "hard_cap" | "hidden" | "identity" | "space" | "connection" | "upstream";

export type VoiceSessionEvents = {
  onTranscript: (role: "member" | "agent", delta: string, order?: string | number) => void;
  onFunctionCall: (call: VoiceFunctionCall) => void;
  onEnded: (reason: VoiceEndReason) => void;
};

export type VoiceSessionDeps = {
  claim?: () => MicrophoneClaim;
  createPeer?: () => RTCPeerConnection;
  requestSession?: (
    body: { sdp_offer: string; tools: unknown[]; playbook: string },
    signal?: AbortSignal,
  ) => Promise<VoiceSessionResponse>;
  playRemote?: (stream: MediaStream) => () => void;
  now?: () => number;
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

/** OpenAI's WebRTC sequence sends the offer only once ICE gathering is complete. */
function iceGathered(pc: RTCPeerConnection, signal: AbortSignal): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const done = (error?: Error) => {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", onState);
      signal.removeEventListener("abort", onAbort);
      if (error) reject(error);
      else resolve();
    };
    const onState = () => {
      if (pc.iceGatheringState === "complete") done();
    };
    const onAbort = () => done(new DOMException("Voice ended while connecting.", "AbortError"));
    const timer = setTimeout(
      () => done(new Error("Voice could not finish gathering network candidates.")),
      VOICE_ICE_GATHER_WAIT_MS,
    );
    pc.addEventListener("icegatheringstatechange", onState);
    signal.addEventListener("abort", onAbort);
  });
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
  // Listen before the first await: loss during setup aborts the offer and frees the mic.
  const setup = new AbortController();
  let session: VoiceSession | null = null;
  gate.onLost(() => {
    if (session) void session.end("identity", { immediate: true });
    else setup.abort();
  });
  const checkSetup = () => {
    if (setup.signal.aborted) throw new DOMException("Voice ended while connecting.", "AbortError");
  };
  try {
    const stream = await claim.open();
    checkSetup();
    peer = (deps.createPeer ?? (() => new RTCPeerConnection()))();
    const pc = peer;
    for (const track of stream.getTracks()) pc.addTrack(track, stream);
    pc.ontrack = (event) => {
      audio.stop?.();
      audio.stop = (deps.playRemote ?? playRemoteAudio)(event.streams[0]);
    };
    const channel = pc.createDataChannel("oai-events");
    await pc.setLocalDescription(await pc.createOffer());
    checkSetup();
    await iceGathered(pc, setup.signal);
    // The local description, unlike the bare offer, carries the gathered candidates.
    const sdp = pc.localDescription?.sdp;
    if (!sdp) throw new Error("Voice has no local session description.");
    const answer = await (deps.requestSession ?? createVoiceSession)(
      { sdp_offer: sdp, tools, playbook: RCP_PLAYBOOK },
      setup.signal,
    );
    checkSetup();
    await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp_answer });
    checkSetup();
    session = startSession(
      answer.limits,
      pc,
      channel,
      claim,
      () => audio.stop?.(),
      events,
      deps.now ?? Date.now,
    );
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
  now: () => number,
): VoiceSession {
  let ending: Promise<void> | null = null;
  let markClosed: () => void = () => {};
  const closed = new Promise<void>((resolve) => {
    markClosed = resolve;
  });
  // Absolute deadlines: a tick that runs late, after the Mac slept or the page was
  // held back, ends the session then rather than granting the missed time.
  const hardCapAt = now() + limits.hard_cap_seconds * 1000;
  let idleAt = now() + limits.idle_seconds * 1000;
  const endIfDue = (): boolean => {
    if (ending) return true;
    const time = now();
    const reason = time >= hardCapAt ? "hard_cap" : time >= idleAt ? "idle" : null;
    if (reason) void end(reason);
    return reason !== null;
  };
  const deadlineTimer = setInterval(endIfDue, VOICE_DEADLINE_TICK_MS);

  // Resume can finish its status reads before WebRTC opens the data channel.
  const pendingCommentary: Record<string, unknown>[] = [];
  channel.onopen = () => {
    if (endIfDue()) return;
    pendingCommentary.splice(0).forEach((event) => channel.send(JSON.stringify(event)));
  };
  const send = (event: Record<string, unknown>) => {
    if (channel.readyState === "open") channel.send(JSON.stringify(event));
  };
  const noteActivity = () => {
    if (endIfDue()) return;
    idleAt = now() + limits.idle_seconds * 1000;
  };

  let finished = false;
  // The first reason wins; the close acknowledgement that follows an End is not a new one.
  let endReason: VoiceEndReason = "connection";
  const finish = () => {
    if (finished) return;
    finished = true;
    clearInterval(deadlineTimer);
    channel.close();
    pc.close();
    stopAudio();
    claim.release();
    events.onEnded(endReason);
  };
  const end = (reason: VoiceEndReason, options: { immediate?: boolean } = {}): Promise<void> => {
    // A page going away cannot wait out a close already in progress.
    if (ending) {
      if (options.immediate) finish();
      return ending;
    }
    endReason = reason;
    clearInterval(deadlineTimer);
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
    let event: { type?: unknown; delta?: unknown; item_id?: unknown };
    try {
      event = JSON.parse(String(message.data));
    } catch {
      return;
    }
    // After End, only the close acknowledgement matters; a late call must not run.
    if (ending && event.type !== "session.closed") return;
    // An event that arrives past a deadline ends the session instead of running.
    if (!ending && endIfDue()) return;
    if (event.type === "session.input_transcript.delta" && typeof event.delta === "string") {
      noteActivity();
      events.onTranscript(
        "member",
        event.delta,
        typeof event.item_id === "string" ? event.item_id : undefined,
      );
    } else if (
      event.type === "session.output_transcript.delta" &&
      typeof event.delta === "string"
    ) {
      // The assistant speaking is activity too; idle must not cut off a long answer.
      noteActivity();
      events.onTranscript(
        "agent",
        event.delta,
        typeof event.item_id === "string" ? event.item_id : undefined,
      );
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
    speak: (text) => {
      if (endIfDue()) return;
      const event = {
        type: "session.commentary.append",
        event_id: eventId(),
        delegation_id: null,
        content: text.slice(0, limits.commentary_max_chars),
      };
      if (channel.readyState === "connecting") pendingCommentary.push(event);
      else send(event);
    },
    noteActivity,
    end,
  };
}

/**
 * A frozen page runs no timers, so the session ends when the page freezes or goes.
 * Hiding also ends it, unless the window keeps running scripts while hidden.
 */
export function endOnPageSuspend(
  end: () => void,
  { keepWhileHidden }: { keepWhileHidden: boolean },
): () => void {
  const onVisibility = () => {
    if (!keepWhileHidden && document.visibilityState === "hidden") end();
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
