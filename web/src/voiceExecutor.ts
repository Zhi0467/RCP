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
    machine_name: string | null;
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
  catalog: () => readonly CatalogTool[];
  resolve: (name: string) => ToolResolution;
  confirmMode: () => VoiceConfirmMode;
  /** Reads current page state; throws when the action cannot be pinned. */
  pin: (name: string, args: Record<string, unknown>) => Promise<VoicePin>;
  /** True on Confirm; false on decline, timeout, or the session ending. */
  requestConfirmation: (pin: VoicePin) => Promise<boolean>;
  onSucceeded?: (name: string, args: Record<string, unknown>, output: string) => void;
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
  const unknownOutcomes = new Set<string>();
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
    const callKey = `${call.name}:${stableJson(args)}`;
    if (unknownOutcomes.has(callKey)) {
      return refusal(
        "unknown_outcome",
        "An identical earlier call has an unknown outcome; check the chat before repeating it.",
      );
    }
    let runArgs = args;
    // An always-confirm tool shows its card even when the member runs without confirming.
    if (tool.alwaysConfirm || (deps.confirmMode() === "tap" && tool.confirm(args))) {
      let pinned: VoicePin;
      try {
        pinned = await deps.pin(call.name, args);
      } catch (error) {
        return refusal("refused", errorText(error));
      }
      if (!(await deps.requestConfirmation(pinned))) {
        return refusal("not_confirmed", "The member did not confirm this action.");
      }
      if (!deps.gate.ok()) return refusal("identity", "RCP signed out; the voice session ended.");
      let current: VoicePin | null = null;
      try {
        current = await deps.pin(call.name, args);
      } catch {
        current = null;
      }
      if (!current || stableJson(current) !== stableJson(pinned)) {
        return refusal("changed", "The project changed after the card was shown; nothing ran.");
      }
      runArgs = pinned.arguments;
    }
    const resolved = deps.resolve(call.name);
    if (!resolved.ok) return refusal("refused", resolved.refusal);
    if (!deps.gate.ok()) return refusal("identity", "RCP signed out; the voice session ended.");
    try {
      const result = await resolved.definition.execute(runArgs);
      const output = result.content.map((item) => item.text).join("\n");
      deps.onSucceeded?.(call.name, args, output);
      return output;
    } catch (error) {
      if (resolved.definition.annotations?.readOnlyHint !== true && outcomeUnknown(error)) {
        unknownOutcomes.add(callKey);
        return refusal("unknown_outcome", "The outcome is unknown; check the chat.");
      }
      return refusal("refused", errorText(error));
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
