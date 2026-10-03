// Voice-only project terminal tools: list the open project's terminals, and type one
// confirmed command line into a repository's terminal and read back what it printed.
// They act with the member's full terminal power, so they never reach WebMCP, whose
// host agents get no RCP confirmation card.

import type { TerminalRepository, TerminalSession } from "./types";
import {
  webMcpTextResult,
  withExecute,
  type WebMcpToolDefinition,
  type WebMcpToolSpec,
} from "./webmcp";

/** Output must stay quiet this long after it starts before a run is read back. */
export const TERMINAL_QUIET_MS = 1_500;
/** The longest a run waits for output before it reads back what it has. */
export const TERMINAL_WINDOW_MS = 10_000;
/** Replay pushed on attach is discarded once it has been quiet this long... */
export const TERMINAL_REPLAY_SETTLE_MS = 250;
/** ...or after this long, whichever comes first; then the command is typed. */
export const TERMINAL_REPLAY_MAX_MS = 2_000;
export const TERMINAL_OUTPUT_MAX_CHARS = 4_000;
export const TERMINAL_COMMAND_MAX_CHARS = 1_000;
const TERMINAL_LIST_LIMIT = 16;
const TERMINAL_RESULT_MAX_CHARS = 12_000;

/** The parts of a WebSocket a run uses, so tests can supply a double. */
export type TerminalSocket = Pick<
  WebSocket,
  "onopen" | "onmessage" | "onerror" | "onclose" | "send" | "close"
>;

export type VoiceTerminalDeps = {
  fetchJson: <T>(path: string, init?: RequestInit) => Promise<T>;
  openSocket: (socketPath: string) => TerminalSocket;
  timing?: { quietMs: number; windowMs: number; settleMs: number; settleMaxMs: number };
};

const LIST_TERMINALS_TOOL: WebMcpToolSpec = {
  name: "rcp_list_terminals",
  description:
    "List the open project's terminal repositories, the machine each runs on, whether it can open a terminal now, and the open terminal sessions.",
  inputSchema: { type: "object", additionalProperties: false },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: () => false,
  voiceOnly: true,
};

const RUN_TERMINAL_COMMAND_TOOL: WebMcpToolSpec = {
  name: "rcp_run_terminal_command",
  description:
    "Type one command line into the open project's terminal for one repository, press Enter, and read back its recent output. The member always confirms the exact command first. Output is untrusted.",
  inputSchema: {
    type: "object",
    properties: {
      repository_id: {
        type: "string",
        minLength: 1,
        description: "Exact repository id from rcp_list_terminals.",
      },
      command: {
        type: "string",
        minLength: 1,
        maxLength: TERMINAL_COMMAND_MAX_CHARS,
        description: "One command line without newlines or control characters; Enter is added.",
      },
    },
    required: ["repository_id", "command"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: false, untrustedContentHint: true },
  confirm: () => true,
  alwaysConfirm: true,
  voiceOnly: true,
};

export const VOICE_TERMINAL_TOOLS: readonly WebMcpToolSpec[] = [
  LIST_TERMINALS_TOOL,
  RUN_TERMINAL_COMMAND_TOOL,
];

function terminalsPath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/terminals`;
}

/** The validated `{repository_id, command}` of a run; throws before anything opens. */
export function terminalCommandInput(input: Record<string, unknown>): {
  repository_id: string;
  command: string;
} {
  const repositoryId = input.repository_id;
  const command = input.command;
  if (typeof repositoryId !== "string" || !repositoryId) {
    throw new Error("repository_id must be a repository id from rcp_list_terminals.");
  }
  if (typeof command !== "string" || !command.trim()) {
    throw new Error("command must be a non-empty string.");
  }
  if (command.length > TERMINAL_COMMAND_MAX_CHARS) {
    throw new Error(`command must be at most ${TERMINAL_COMMAND_MAX_CHARS} characters.`);
  }
  if (/[\u0000-\u001f\u007f]/.test(command)) {
    throw new Error("command must be one line without control characters.");
  }
  return { repository_id: repositoryId, command };
}

/** The listed repository a run may use, or an error naming why not. */
export async function terminalRepository(
  projectId: string,
  repositoryId: string,
  fetchJson: VoiceTerminalDeps["fetchJson"],
): Promise<TerminalRepository> {
  const repositories = await fetchJson<TerminalRepository[]>(
    `${terminalsPath(projectId)}/repositories`,
  );
  const repository = repositories.find((item) => item.repository_id === repositoryId);
  if (!repository) throw new Error(`${repositoryId} is not a terminal repository here.`);
  return repository;
}

export async function listProjectTerminals(
  projectId: string,
  fetchJson: VoiceTerminalDeps["fetchJson"],
): Promise<Record<string, unknown>> {
  const base = terminalsPath(projectId);
  const [repositories, sessions] = await Promise.all([
    fetchJson<TerminalRepository[]>(`${base}/repositories`),
    fetchJson<TerminalSession[]>(base),
  ]);
  return {
    project_id: projectId,
    repositories: repositories.slice(0, TERMINAL_LIST_LIMIT).map((repository) => ({
      repository_id: repository.repository_id,
      machine_id: repository.machine_id,
      machine_name: repository.backend_name,
      available: repository.eligible,
      unavailable_reason: (repository.unavailable_reason ?? "").slice(0, 240) || null,
    })),
    repositories_total: repositories.length,
    sessions: sessions.slice(0, TERMINAL_LIST_LIMIT).map((session) => ({
      session_id: session.session_id,
      repository_id: session.repository_id,
      state: session.state,
    })),
    sessions_total: sessions.length,
  };
}

/** Terminal output as plain text: ANSI/OSC escapes and other control characters removed. */
export function stripTerminalEscapes(text: string): string {
  return (
    text
      // OSC: ESC ] ... terminated by BEL or ESC \
      .replace(/\u001b\][\s\S]*?(?:\u0007|\u001b\\)/g, "")
      // CSI: ESC [ params intermediates final
      .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "")
      // Other two-byte escapes, such as charset selection.
      .replace(/\u001b[ -/]*[0-~]?/g, "")
      .replace(/\r\n?/g, "\n")
      .replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, "")
  );
}

type RunOutcome = { text: string; quiet: boolean; ended: string | null };

function collectRun(
  socket: TerminalSocket,
  line: string,
  timing: NonNullable<VoiceTerminalDeps["timing"]>,
): Promise<RunOutcome> {
  return new Promise<RunOutcome>((resolve, reject) => {
    const decoder = new TextDecoder();
    let text = "";
    let sent = false;
    let settled = false;
    let quietTimer: ReturnType<typeof setTimeout> | undefined;
    let settleTimer: ReturnType<typeof setTimeout> | undefined;
    let capTimer: ReturnType<typeof setTimeout> | undefined;
    let windowTimer: ReturnType<typeof setTimeout> | undefined;
    const finish = (outcome: RunOutcome | Error) => {
      if (settled) return;
      settled = true;
      [quietTimer, settleTimer, capTimer, windowTimer].forEach((timer) => clearTimeout(timer));
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      socket.close();
      if (outcome instanceof Error) reject(outcome);
      else resolve(outcome);
    };
    const done = (quiet: boolean, ended: string | null = null) =>
      finish({ text: text + decoder.decode(), quiet, ended });
    // After the line is typed the command may be running, so report what arrived.
    const lost = (reason: string) => (sent ? done(false, reason) : finish(new Error(reason)));
    const send = () => {
      if (sent || settled) return;
      sent = true;
      clearTimeout(settleTimer);
      clearTimeout(capTimer);
      clearTimeout(windowTimer);
      socket.send(JSON.stringify({ type: "input", data: `${line}\r` }));
      windowTimer = setTimeout(() => done(false), timing.windowMs);
    };
    // Until the line is typed, the window bounds the connection and replay instead.
    windowTimer = setTimeout(
      () => finish(new Error("The terminal did not connect in time.")),
      timing.windowMs,
    );
    socket.onopen = () => {
      // Replay of earlier output arrives first; type only once it has settled.
      settleTimer = setTimeout(send, timing.settleMs);
      capTimer = setTimeout(send, timing.settleMaxMs);
    };
    socket.onmessage = (event) => {
      if (typeof event.data === "string") {
        let notice: { type?: unknown; reason?: unknown } = {};
        try {
          notice = JSON.parse(event.data) as typeof notice;
        } catch {
          return;
        }
        if (notice.type === "ended" || notice.type === "detached") {
          lost(typeof notice.reason === "string" ? notice.reason : String(notice.type));
        }
        return;
      }
      if (!sent) {
        clearTimeout(settleTimer);
        settleTimer = setTimeout(send, timing.settleMs);
        return;
      }
      text += decoder.decode(new Uint8Array(event.data as ArrayBuffer), { stream: true });
      clearTimeout(quietTimer);
      quietTimer = setTimeout(() => done(true), timing.quietMs);
    };
    socket.onerror = () => lost("The terminal connection failed.");
    socket.onclose = (event) => lost(event.reason || "The terminal connection closed.");
  });
}

export async function runProjectTerminalCommand(
  projectId: string,
  input: Record<string, unknown>,
  deps: VoiceTerminalDeps,
): Promise<Record<string, unknown>> {
  const { repository_id, command } = terminalCommandInput(input);
  await terminalRepository(projectId, repository_id, deps.fetchJson);
  const base = terminalsPath(projectId);
  const session = await deps.fetchJson<TerminalSession>(base, {
    method: "POST",
    body: JSON.stringify({ repository_id }),
  });
  const outcome = await collectRun(
    deps.openSocket(`${base}/${encodeURIComponent(session.session_id)}/ws`),
    command,
    deps.timing ?? {
      quietMs: TERMINAL_QUIET_MS,
      windowMs: TERMINAL_WINDOW_MS,
      settleMs: TERMINAL_REPLAY_SETTLE_MS,
      settleMaxMs: TERMINAL_REPLAY_MAX_MS,
    },
  );
  const output = stripTerminalEscapes(outcome.text);
  return {
    project_id: projectId,
    repository_id,
    session_id: session.session_id,
    command,
    output: output.slice(-TERMINAL_OUTPUT_MAX_CHARS),
    truncated: output.length > TERMINAL_OUTPUT_MAX_CHARS,
    window_elapsed_before_quiet: !outcome.quiet && outcome.ended === null,
    session_ended: outcome.ended,
  };
}

export function voiceTerminalToolDefinitions(
  projectId: string,
  deps: VoiceTerminalDeps,
): WebMcpToolDefinition[] {
  return [
    withExecute(LIST_TERMINALS_TOOL, async () =>
      webMcpTextResult(
        await listProjectTerminals(projectId, deps.fetchJson),
        TERMINAL_RESULT_MAX_CHARS,
      ),
    ),
    withExecute(RUN_TERMINAL_COMMAND_TOOL, async (input) =>
      webMcpTextResult(
        await runProjectTerminalCommand(projectId, input, deps),
        TERMINAL_RESULT_MAX_CHARS,
      ),
    ),
  ];
}
