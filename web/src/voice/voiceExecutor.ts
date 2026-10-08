// Runs a voice session's function calls as the member, through the shared tool catalog.
// Pure: page state, confirmation cards, and identity come in as dependencies.

import type { CatalogTool, ToolResolution } from "./toolCatalog";

export type VoiceConfirmMode = "tap" | "none";

export type VoiceFunctionCall = { call_id: string; name: string; arguments: string };

/** The exact action a confirmation card shows and Confirm re-checks. */
export type VoicePin = {
  tool: string;
  project_id: string;
  project_name: string;
  graph_target: { kind: string; branch_id: string | null };
  /** The arguments that run after Confirm, including an explicit budget. */
  arguments: Record<string, unknown>;
  budget: number | null;
  mode: string | null;
  provider_profile: Record<string, unknown> | null;
  starting_instruction: string | null;
  /** The truth scope an Experiment start sends. */
  truth_scope: string[] | null;
  /** The repository terminal a command runs in, as listed, and the exact command. */
  terminal: {
    repository_id: string;
    machine_id: string;
    containment: string | null;
    command: string;
  } | null;
};

/** The page's verified identity, as the voice session sees it. Loss is final. */
export type VoiceIdentityGate = {
  ok: () => boolean;
  lose: () => void;
  onLost: (listener: () => void) => void;
};

export function createIdentityGate(): VoiceIdentityGate {
  let lost = false;
  const listeners: Array<() => void> = [];
  return {
    ok: () => !lost,
    lose: () => {
      if (lost) return;
      lost = true;
      listeners.splice(0).forEach((listener) => listener());
    },
    onLost: (listener) => {
      if (lost) listener();
      else listeners.push(listener);
    },
  };
}

export type VoiceExecutorDeps = {
  gate: VoiceIdentityGate;
  initialReceipts?: readonly VoiceReceipt[];
  target?: () => VoiceReceiptTarget | null;
  saveReceipt?: (receipt: VoiceReceipt) => Promise<void>;
  catalog: () => readonly CatalogTool[];
  resolve: (name: string) => ToolResolution;
  confirmMode: () => VoiceConfirmMode;
  /** Reads current page state; throws when the action cannot be pinned. */
  pin: (name: string, args: Record<string, unknown>) => Promise<VoicePin>;
  /** True on Confirm; false on decline, timeout, or the session ending. */
  requestConfirmation: (pin: VoicePin) => Promise<boolean>;
  onSucceeded?: (name: string, args: Record<string, unknown>, output: string) => void;
};

export type VoiceReceiptTarget = {
  project_id: string;
  project_name: string;
  graph_target: { kind: string; branch_id: string | null };
};
export type VoiceReceipt = {
  tool: string;
  target: VoiceReceiptTarget;
  call_id: string;
  argument_fingerprint: string;
  outcome: "accepted" | "refused" | "unknown";
  task_id?: string | null;
  episode_id?: string | null;
};

export type VoiceRefusalCode =
  | "busy"
  | "identity"
  | "unknown_tool"
  | "bad_arguments"
  | "unknown_outcome"
  | "not_confirmed"
  | "changed"
  | "refused";

function refusal(code: VoiceRefusalCode, error: string): string {
  return JSON.stringify({ ok: false, code, error });
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

// A dropped request or a server failure may or may not have taken effect.
function outcomeUnknown(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === "number" && status >= 500;
}

/** `run` returns the `function_call_output` text, or null when the call was already handled. */
export function createVoiceExecutor(deps: VoiceExecutorDeps) {
  const seenCallIds = new Set<string>();
  const receiptKey = (receipt: VoiceReceipt) =>
    stableJson([
      receipt.tool,
      receipt.target.project_id,
      receipt.target.graph_target,
      receipt.argument_fingerprint,
    ]);
  const unknownOutcomes = new Set(
    (deps.initialReceipts ?? []).filter((item) => item.outcome === "unknown").map(receiptKey),
  );
  let busy = false;

  async function runOne(call: VoiceFunctionCall): Promise<string> {
    if (!deps.gate.ok()) return refusal("identity", "RCP signed out; the voice session ended.");
    const tool = deps.catalog().find((candidate) => candidate.name === call.name);
    if (!tool) return refusal("unknown_tool", `${call.name} is not an RCP tool.`);
    let args: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(call.arguments || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
      args = parsed as Record<string, unknown>;
    } catch {
      return refusal("bad_arguments", "Arguments must be a JSON object.");
    }
    const target = deps.target?.() ?? null;
    const fingerprint = Array.from(
      new Uint8Array(
        await crypto.subtle.digest("SHA-256", new TextEncoder().encode(stableJson(args))),
      ),
      (byte) => byte.toString(16).padStart(2, "0"),
    ).join("");
    const receipt: VoiceReceipt | null = target
      ? {
          tool: call.name,
          target,
          call_id: call.call_id,
          argument_fingerprint: fingerprint,
          outcome: "unknown",
        }
      : null;
    const callKey = receipt ? receiptKey(receipt) : `${call.name}:${fingerprint}`;
    const available = deps.resolve(call.name);
    const isWrite = available.ok && available.definition.annotations?.readOnlyHint !== true;
    const refuse = async (code: VoiceRefusalCode, error: string) => {
      if (receipt && isWrite) await deps.saveReceipt?.({ ...receipt, outcome: "refused" });
      unknownOutcomes.delete(callKey);
      return refusal(code, error);
    };
    if (unknownOutcomes.has(callKey)) {
      return refusal(
        "unknown_outcome",
        "An identical earlier call has an unknown outcome; check the chat before repeating it.",
      );
    }
    // A tool the page cannot run now is refused before any card is shown.
    if (!available.ok) return refuse("refused", available.refusal);
    let runArgs = args;
    // An always-confirm tool shows its card even when the member runs without confirming.
    if (tool.alwaysConfirm || (deps.confirmMode() === "tap" && tool.confirm(args))) {
      let pinned: VoicePin;
      try {
        pinned = await deps.pin(call.name, args);
      } catch (error) {
        return refuse("refused", errorText(error));
      }
      if (!(await deps.requestConfirmation(pinned))) {
        return refuse("not_confirmed", "The member did not confirm this action.");
      }
      if (!deps.gate.ok()) return refuse("identity", "RCP signed out; the voice session ended.");
      let current: VoicePin | null = null;
      try {
        current = await deps.pin(call.name, args);
      } catch {
        current = null;
      }
      if (!current || stableJson(current) !== stableJson(pinned)) {
        return refuse("changed", "The project changed after the card was shown; nothing ran.");
      }
      runArgs = pinned.arguments;
    }
    const resolved = deps.resolve(call.name);
    if (!resolved.ok) return refuse("refused", resolved.refusal);
    if (!deps.gate.ok()) return refuse("identity", "RCP signed out; the voice session ended.");
    if (deps.target && stableJson(deps.target()) !== stableJson(target)) {
      return refuse("changed", "The project changed; nothing ran.");
    }
    if (isWrite && deps.saveReceipt) {
      if (!receipt) return refusal("refused", "No project target is available.");
      await deps.saveReceipt(receipt);
      unknownOutcomes.add(callKey);
      if (!deps.gate.ok()) return refusal("identity", "RCP signed out; the voice session ended.");
      if (deps.target && stableJson(deps.target()) !== stableJson(target)) {
        return refuse("changed", "The project changed; nothing ran.");
      }
    }
    try {
      const result = await resolved.definition.execute(runArgs);
      const output = result.content.map((item) => item.text).join("\n");
      if (receipt && isWrite) {
        const watch = voiceWatchFromResult(call.name, args, output, () => target!.project_name);
        try {
          await deps.saveReceipt?.({
            ...receipt,
            outcome: voiceCallOutcome(output).ok ? "accepted" : "refused",
            ...(watch?.record === "task" ? { task_id: watch.id } : {}),
            ...(watch?.record === "episode" ? { episode_id: watch.id } : {}),
          });
        } catch {
          unknownOutcomes.add(callKey);
          return refusal(
            "unknown_outcome",
            "The action receipt could not be saved; check the chat.",
          );
        }
        unknownOutcomes.delete(callKey);
      }
      deps.onSucceeded?.(call.name, args, output);
      return output;
    } catch (error) {
      if (resolved.definition.annotations?.readOnlyHint !== true && outcomeUnknown(error)) {
        unknownOutcomes.add(callKey);
        return refusal("unknown_outcome", "The outcome is unknown; check the chat.");
      }
      return refuse("refused", errorText(error));
    }
  }

  return {
    async run(call: VoiceFunctionCall): Promise<string | null> {
      if (seenCallIds.has(call.call_id)) return null;
      seenCallIds.add(call.call_id);
      // parallel_tool_calls is false upstream, so a second call while one runs is a fault.
      if (busy) return refusal("busy", "One RCP action runs at a time.");
      busy = true;
      try {
        return await runOne(call);
      } catch (error) {
        return refusal("refused", errorText(error));
      } finally {
        busy = false;
      }
    },
  };
}

export type VoiceWatchKind = "work_turn" | "experiment" | "auto_research";
export type VoiceWatchStatus = "finished" | "needs_you";

/** A task or episode this session started, polled by exact id. */
export type VoiceWatch = {
  kind: VoiceWatchKind;
  record: "task" | "episode";
  id: string;
  project_id: string;
  project_name: string;
  last: VoiceWatchStatus | null;
};

/** What to watch after a successful start, from the tool's own structured result. */
/** What a call's output says happened, for the panel's activity line. */
export function voiceCallOutcome(output: string): {
  ok: boolean;
  code: string | null;
  error: string | null;
} {
  try {
    const parsed = JSON.parse(output) as { ok?: unknown; code?: unknown; error?: unknown };
    if (parsed && parsed.ok === false) {
      return {
        ok: false,
        code: typeof parsed.code === "string" ? parsed.code : null,
        error: typeof parsed.error === "string" ? parsed.error : null,
      };
    }
  } catch {
    // A plain-text result is a success.
  }
  return { ok: true, code: null, error: null };
}

export function voiceWatchFromResult(
  name: string,
  args: Record<string, unknown>,
  output: string,
  projectName: (projectId: string) => string,
): VoiceWatch | null {
  let result: Record<string, unknown>;
  try {
    result = JSON.parse(output) as Record<string, unknown>;
  } catch {
    return null;
  }
  const projectId = typeof result.project_id === "string" ? result.project_id : null;
  const text = (key: string) => (typeof result[key] === "string" ? (result[key] as string) : null);
  if (!projectId) return null;
  const base = { project_id: projectId, project_name: projectName(projectId), last: null };
  if (name === "rcp_send_conversation_message" && args.mode === "work" && text("task_id")) {
    return { ...base, kind: "work_turn", record: "task", id: text("task_id")! };
  }
  if (name === "rcp_start_experiment") {
    if (text("episode_id"))
      return { ...base, kind: "experiment", record: "episode", id: text("episode_id")! };
    if (text("task_id"))
      return { ...base, kind: "experiment", record: "task", id: text("task_id")! };
  }
  if (name === "rcp_authorize_auto_research" && text("episode_id")) {
    return { ...base, kind: "auto_research", record: "episode", id: text("episode_id")! };
  }
  return null;
}

export function taskWatchStatus(task: {
  awaiting_human: boolean;
  settled: boolean;
}): VoiceWatchStatus | null {
  if (task.awaiting_human) return "needs_you";
  return task.settled ? "finished" : null;
}

export function episodeWatchStatus(episode: { run_section: string }): VoiceWatchStatus | null {
  if (episode.run_section === "actionable") return "needs_you";
  return episode.run_section === "completed" ? "finished" : null;
}

const KIND_TEXT: Record<VoiceWatchKind, string> = {
  work_turn: "The Work turn",
  experiment: "The Experiment episode",
  auto_research: "The Auto-research episode",
};

const STATUS_TEXT: Record<VoiceWatchStatus, string> = {
  finished: "has finished",
  needs_you: "needs you",
};

/**
 * The one spoken update about this session's own work: a fixed template of kind,
 * project name, and status. Provider answers and other authored text never enter it.
 */
export function voiceCommentary(
  kind: VoiceWatchKind,
  projectName: string,
  status: VoiceWatchStatus,
  maxChars: number,
): string {
  const frame = (name: string) => `${KIND_TEXT[kind]} in ${name} ${STATUS_TEXT[status]}.`;
  const room = Math.max(0, maxChars - frame("").length);
  const name =
    projectName.length <= room ? projectName : `${projectName.slice(0, Math.max(0, room - 1))}…`;
  return frame(name).slice(0, maxChars);
}

export type VoiceTranscriptEntry = {
  speaker: "member" | "agent";
  text: string;
  provider_order?: number | string | null;
  source?: string | null;
};

/** The persisted transcript is independent of the small panel display buffer. */
export function appendVoiceTranscript(
  entries: readonly VoiceTranscriptEntry[],
  speaker: VoiceTranscriptEntry["speaker"],
  delta: string,
  limits: { entry_bytes: number; session_bytes: number },
  provider_order?: number | string | null,
  source?: string | null,
): VoiceTranscriptEntry[] {
  const encoder = new TextEncoder();
  const result = entries.map((entry) => ({ ...entry }));
  const last = result.at(-1);
  const tail =
    last?.speaker === speaker &&
    (last.provider_order ?? null) === (provider_order ?? null) &&
    (last.source ?? null) === (source ?? null)
      ? result.pop()!.text + delta
      : delta;
  // Iterate code points so byte truncation never splits a surrogate pair.
  let text = "";
  let bytes = 0;
  const chunks: string[] = [];
  for (const character of tail) {
    const size = encoder.encode(character).length;
    if (size > limits.entry_bytes) continue;
    if (bytes + size > limits.entry_bytes) {
      chunks.push(text);
      text = "";
      bytes = 0;
    }
    text += character;
    bytes += size;
  }
  if (text) chunks.push(text);
  result.push(
    ...chunks.map((text) => ({
      speaker,
      text,
      ...(provider_order === undefined ? {} : { provider_order }),
      ...(source === undefined ? {} : { source }),
    })),
  );
  while (result.length && encoder.encode(JSON.stringify(result)).length > limits.session_bytes)
    result.shift();
  return result;
}

/** At most one save is in flight; a newer snapshot replaces the pending one. */
export function createVoiceSaveQueue<T>(
  save: (snapshot: T) => Promise<void>,
  isCurrent: () => boolean,
) {
  let pending: { snapshot: T; resolve: () => void; reject: (error: unknown) => void }[] = [];
  let running: Promise<void> | null = null;
  async function drain() {
    while (pending.length) {
      const batch = pending;
      pending = [];
      try {
        if (!isCurrent()) throw new Error("Voice session identity changed.");
        await save(batch.at(-1)!.snapshot);
        batch.forEach(({ resolve }) => resolve());
      } catch (error) {
        batch.concat(pending).forEach(({ reject }) => reject(error));
        pending = [];
        return;
      }
    }
  }
  function start() {
    if (!running)
      running = Promise.resolve()
        .then(drain)
        .finally(() => {
          running = null;
          if (pending.length) start();
        });
  }
  return {
    save(snapshot: T): Promise<void> {
      const acknowledged = new Promise<void>((resolve, reject) =>
        pending.push({ snapshot, resolve, reject }),
      );
      start();
      return acknowledged;
    },
    async flush() {
      while (running) await running;
    },
  };
}

/** A finish offers access; only the explicit page-tool call can open the result. */
export function createFinishedResultOffer(deps: {
  currentTarget: () => VoiceReceiptTarget | null;
  list: (
    watch: VoiceWatch,
  ) => Promise<{ viewer_id: string; can_open: boolean; can_open_pdf?: boolean }[]>;
  open: (viewerId: string) => Promise<void>;
}) {
  let offered: { watch: VoiceWatch; target: VoiceReceiptTarget } | null = null;
  const sameTarget = (target: VoiceReceiptTarget) => {
    const current = deps.currentTarget();
    return (
      current?.project_id === target.project_id &&
      stableJson(current.graph_target) === stableJson(target.graph_target)
    );
  };
  return {
    offer(watch: VoiceWatch, target: VoiceReceiptTarget) {
      offered = { watch, target };
    },
    async open() {
      const captured = offered;
      if (!captured || !sameTarget(captured.target))
        throw new Error("The offered result is not in the current project and graph.");
      const artifacts = await deps.list(captured.watch);
      if (offered !== captured || !sameTarget(captured.target))
        throw new Error("The project or graph changed.");
      const eligible = artifacts.filter((artifact) => artifact.can_open || artifact.can_open_pdf);
      if (!eligible.length) throw new Error("This task has no result available in a viewer.");
      for (const artifact of eligible) {
        if (offered !== captured || !sameTarget(captured.target))
          throw new Error("The project or graph changed.");
        await deps.open(artifact.viewer_id);
      }
      offered = null;
      return eligible.length;
    },
  };
}

export function voiceWatchFromReceipt(receipt: VoiceReceipt): VoiceWatch | null {
  if (receipt.outcome !== "accepted") return null;
  return voiceWatchFromResult(
    receipt.tool,
    { mode: "work" },
    JSON.stringify({
      project_id: receipt.target.project_id,
      task_id: receipt.task_id,
      episode_id: receipt.episode_id,
    }),
    () => receipt.target.project_name,
  );
}
