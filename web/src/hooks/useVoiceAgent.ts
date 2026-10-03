import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import {
  api,
  ApiError,
  loadEpisodes,
  loadVoiceSettings,
  registerAccessLossHandler,
  saveVoiceSettings,
} from "../api";
import { serviceConnectionFailure } from "../dictation";
import { errorMessage } from "../errors";
import { MicrophoneBusyError } from "../microphone";
import { catalog, catalogAsFunctionTools, resolve } from "../toolCatalog";
import type { AgentProfile, AgentTask, ProjectSnapshot, VoiceSettings } from "../types";
import {
  createIdentityGate,
  createVoiceExecutor,
  episodeWatchStatus,
  taskWatchStatus,
  voiceCommentary,
  voiceWatchFromResult,
  type VoiceConfirmMode,
  type VoiceIdentityGate,
  type VoicePin,
  type VoiceWatch,
  type VoiceWatchStatus,
} from "../voiceExecutor";
import {
  endOnPageSuspend,
  openVoiceSession,
  type VoiceEndReason,
  type VoiceSession,
} from "../voiceSession";
import { conversationSendTarget, type WebMcpConversationSource } from "../webmcp";

const VOICE_WATCH_POLL_MS = 10_000;
const VOICE_TRANSCRIPT_LINES = 40;
const VOICE_CARD_PROMPT = "This needs your tap: press Confirm on the card on screen, or Decline.";

/** The open project as the WebMCP surface sees it, read at call and Confirm time. */
export type VoicePageState = {
  project: ProjectSnapshot | null;
  tasks: AgentTask[];
  conversationSource: WebMcpConversationSource;
  /** The page's run scope; empty means the project default. */
  runScope: string[];
};

export type VoiceTranscriptLine = { role: "member" | "agent"; text: string };

export type VoiceProblem = { code: string | null; text: string };

function runProfile(profile: AgentProfile | undefined): Record<string, unknown> | null {
  if (!profile) return null;
  return {
    provider: profile.provider,
    model: profile.model,
    reasoning: profile.reasoning,
    run_on: profile.run_on,
  };
}

/** The exact action a card pins, from current page state. */
async function buildVoicePin(
  name: string,
  args: Record<string, unknown>,
  page: VoicePageState,
): Promise<VoicePin> {
  const project = page.project;
  if (!project) throw new Error("Open a project first.");
  const base = {
    tool: name,
    project_id: project.id,
    project_name: project.name,
    graph_target: {
      kind: project.graph_target?.kind ?? "main",
      branch_id: project.graph_target?.kind === "branch" ? project.graph_target.branch_id : null,
    },
    budget: null,
    mode: null,
    provider_profile: null,
    starting_instruction: null,
    truth_scope: null,
  };
  if (name === "rcp_start_experiment") {
    const id = String(args.experiment_id ?? "");
    const node = project.graph.nodes[id];
    if (!node || node.type !== "experiment") throw new Error(`${id} is not an Experiment here.`);
    const budget = node.invocation_ceiling;
    if (typeof budget !== "number") throw new Error(`Experiment ${id} has no invocation ceiling.`);
    return {
      ...base,
      arguments: { experiment_id: id, invocation_ceiling: budget },
      budget,
      provider_profile: runProfile(project.agent_profiles.node_chat),
      // The scope the start request will send, so a change invalidates the card.
      truth_scope: page.runScope.length ? page.runScope : project.default_run_truth_scope,
    };
  }
  if (name === "rcp_authorize_auto_research") {
    const budget = args.invocation_ceiling;
    if (typeof budget !== "number" || !Number.isSafeInteger(budget) || budget < 1) {
      throw new Error("invocation_ceiling must be an integer of at least 1.");
    }
    const instruction =
      typeof args.starting_instruction === "string" && args.starting_instruction.trim()
        ? args.starting_instruction.trim()
        : null;
    const codeWorktree = args.code_worktree ?? true;
    return {
      ...base,
      arguments: {
        invocation_ceiling: budget,
        ...(instruction ? { starting_instruction: instruction } : {}),
        code_worktree: codeWorktree,
      },
      budget,
      starting_instruction: instruction,
      provider_profile: runProfile(project.agent_profiles.orchestrator),
    };
  }
  if (name === "rcp_send_conversation_message") {
    const target = await conversationSendTarget(project, page.tasks, args, page.conversationSource);
    return {
      ...base,
      arguments: { ...args },
      mode: typeof args.mode === "string" ? args.mode : null,
      provider_profile: { ...target.config, conversation: target.surface },
    };
  }
  throw new Error(`${name} has no confirmation card.`);
}

function appendTranscript(
  lines: VoiceTranscriptLine[],
  role: VoiceTranscriptLine["role"],
  delta: string,
): VoiceTranscriptLine[] {
  const last = lines[lines.length - 1];
  const next =
    last?.role === role
      ? [...lines.slice(0, -1), { role, text: last.text + delta }]
      : [...lines, { role, text: delta }];
  return next.slice(-VOICE_TRANSCRIPT_LINES);
}

const END_NOTICES: Partial<Record<VoiceEndReason, string>> = {
  idle: "Voice ended after a quiet stretch.",
  hard_cap: "Voice ended at its time limit.",
  connection: "Voice ended: the connection dropped.",
  upstream: "Voice ended by OpenAI.",
};

function openFailure(failure: unknown): VoiceProblem {
  if (failure instanceof MicrophoneBusyError)
    return { code: "microphone_busy", text: failure.message };
  let code: string | null = null;
  if (failure instanceof ApiError) {
    try {
      const detail = JSON.parse(failure.message) as { code?: unknown };
      code = typeof detail.code === "string" ? detail.code : null;
    } catch {
      code = null;
    }
  }
  return { code, text: serviceConnectionFailure(failure) ?? errorMessage(failure) };
}

/**
 * The page's one voice session: open on a click, end on a click or any lifetime
 * rule, run calls through the executor, and speak about this session's own work.
 */
export function useVoiceAgent({
  ready,
  spaceId,
  page,
}: {
  /** The same verified identity and team-session state that gates WebMCP. */
  ready: boolean;
  spaceId: string | null;
  page: RefObject<VoicePageState>;
}) {
  const [phase, setPhase] = useState<"idle" | "starting" | "open">("idle");
  const [problem, setProblem] = useState<VoiceProblem | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [transcript, setTranscript] = useState<VoiceTranscriptLine[]>([]);
  const [card, setCard] = useState<VoicePin | null>(null);
  const [settings, setSettings] = useState<VoiceSettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const sessionRef = useRef<VoiceSession | null>(null);
  const startingRef = useRef(false);
  const gateRef = useRef<VoiceIdentityGate | null>(null);
  const settingsRef = useRef<VoiceSettings | null>(null);
  const watchesRef = useRef<VoiceWatch[]>([]);
  const cardRef = useRef<{ settle: (confirmed: boolean) => void } | null>(null);
  const readyRef = useRef(ready);
  readyRef.current = ready;

  const end = useCallback((reason: VoiceEndReason = "member", immediate = false) => {
    // An open session keeps this reason and its bounded close, and loses the gate as it
    // ends; an open still in flight sees the lost gate and ends itself when it lands.
    // A card on screen is declined now, not after the close wait.
    cardRef.current?.settle(false);
    if (sessionRef.current) void sessionRef.current.end(reason, { immediate });
    else gateRef.current?.lose();
  }, []);

  const requestConfirmation = useCallback((pin: VoicePin): Promise<boolean> => {
    cardRef.current?.settle(false);
    const seconds = sessionRef.current?.limits.confirm_timeout_seconds ?? 0;
    return new Promise<boolean>((resolvePromise) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const entry = {
        settle: (confirmed: boolean) => {
          clearTimeout(timer);
          if (cardRef.current === entry) {
            cardRef.current = null;
            setCard(null);
          }
          resolvePromise(confirmed);
        },
      };
      timer = setTimeout(() => entry.settle(false), seconds * 1000);
      cardRef.current = entry;
      setCard(pin);
      // The member may be listening, not looking; a fixed line points at the card.
      sessionRef.current?.speak(VOICE_CARD_PROMPT);
    });
  }, []);

  const open = useCallback(async () => {
    if (sessionRef.current || startingRef.current || !readyRef.current) return;
    startingRef.current = true;
    const gate = createIdentityGate();
    gateRef.current = gate;
    watchesRef.current = [];
    setProblem(null);
    setTranscript([]);
    setPhase("starting");
    // A previous session's choice, possibly another member's, never carries over.
    settingsRef.current = null;
    setSettings(null);
    setSettingsError(null);
    void loadVoiceSettings()
      .then((loaded) => {
        if (gateRef.current !== gate || !gate.ok()) return;
        settingsRef.current = loaded;
        setSettings(loaded);
      })
      .catch((failure) => {
        if (gateRef.current === gate) setSettingsError(errorMessage(failure));
      });
    // Project names as seen when each call was made; the member may navigate mid-call.
    const projectNames = new Map<string, string>();
    const executor = createVoiceExecutor({
      gate,
      catalog,
      resolve,
      // Until the member's choice loads, every gated call waits for a tap.
      confirmMode: (): VoiceConfirmMode => settingsRef.current?.confirm ?? "tap",
      pin: (name, args) => buildVoicePin(name, args, page.current),
      requestConfirmation,
      onSucceeded: (name, args, output) => {
        const watch = voiceWatchFromResult(name, args, output, (id) => projectNames.get(id) ?? "");
        if (watch) watchesRef.current.push(watch);
      },
    });
    let session: VoiceSession;
    try {
      session = await openVoiceSession(
        catalogAsFunctionTools(),
        {
          onTranscript: (role, delta) =>
            setTranscript((lines) => appendTranscript(lines, role, delta)),
          onFunctionCall: (call) => {
            const origin = page.current.project;
            if (origin) projectNames.set(origin.id, origin.name);
            void executor.run(call).then((output) => {
              if (output !== null) sessionRef.current?.sendFunctionOutput(call.call_id, output);
            });
          },
          onEnded: (reason) => {
            if (gateRef.current === gate) gateRef.current = null;
            gate.lose();
            sessionRef.current = null;
            cardRef.current?.settle(false);
            watchesRef.current = [];
            setPhase("idle");
            setStartedAt(null);
            setTranscript([]);
            const notice = END_NOTICES[reason];
            setProblem(notice ? { code: null, text: notice } : null);
          },
        },
        gate,
      );
    } catch (failure) {
      if (gateRef.current === gate) gateRef.current = null;
      setPhase("idle");
      setProblem(openFailure(failure));
      return;
    } finally {
      startingRef.current = false;
    }
    // Identity loss, or an End pressed while the offer was in flight, has ended it already.
    if (!readyRef.current) gate.lose();
    if (!gate.ok()) return;
    sessionRef.current = session;
    setStartedAt(Date.now());
    setPhase("open");
  }, [page, requestConfirmation]);

  const setConfirmMode = useCallback(async (confirm: VoiceConfirmMode) => {
    // A response that lands after this session ended, or another member's opened, is dropped.
    const gate = gateRef.current;
    const live = () => gate !== null && gateRef.current === gate && gate.ok();
    setSettingsError(null);
    try {
      const current = settingsRef.current ?? (await loadVoiceSettings());
      if (!live()) return;
      // The choice shows at once; Tap also applies at once, Run without confirming once saved.
      setSettings({ ...current, confirm });
      if (confirm === "tap") settingsRef.current = { ...current, confirm };
      const saved = await saveVoiceSettings({ confirm });
      if (!live()) return;
      settingsRef.current = saved;
      setSettings(saved);
    } catch (failure) {
      if (!live()) return;
      setSettings(settingsRef.current);
      setSettingsError(errorMessage(failure));
    }
  }, []);

  const active = phase !== "idle";
  useEffect(() => {
    if (!ready) gateRef.current?.lose();
  }, [ready]);
  useEffect(() => () => end("space", true), [end, spaceId]);
  useEffect(() => {
    if (!active) return;
    registerAccessLossHandler(() => gateRef.current?.lose());
    const stopSuspend = endOnPageSuspend(() => end("hidden", true));
    return () => {
      registerAccessLossHandler(null);
      stopSuspend();
    };
  }, [active, end]);

  // Speak first only about this session's own starts, wherever the member has gone.
  useEffect(() => {
    if (phase !== "open") return;
    let polling = false;
    const poll = async () => {
      const session = sessionRef.current;
      if (polling || !session) return;
      polling = true;
      try {
        for (const watch of [...watchesRef.current]) {
          const drop = () => {
            watchesRef.current = watchesRef.current.filter((item) => item !== watch);
          };
          const base = `/api/projects/${encodeURIComponent(watch.project_id)}`;
          let status: VoiceWatchStatus | null;
          try {
            if (watch.record === "task") {
              status = taskWatchStatus(
                await api<AgentTask>(`${base}/tasks/${encodeURIComponent(watch.id)}`),
              );
            } else {
              const episode = (await loadEpisodes(base, undefined, watch.id))[0];
              if (!episode) {
                drop();
                continue;
              }
              status = episodeWatchStatus(episode);
            }
          } catch (failure) {
            if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) drop();
            continue;
          }
          if (status && status !== watch.last && sessionRef.current === session) {
            watch.last = status;
            session.speak(
              voiceCommentary(
                watch.kind,
                watch.project_name,
                status,
                session.limits.commentary_max_chars,
              ),
            );
          }
          if (status === "finished") drop();
        }
      } finally {
        polling = false;
      }
    };
    const timer = window.setInterval(() => void poll(), VOICE_WATCH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [phase]);

  return {
    phase,
    problem,
    startedAt,
    transcript,
    card,
    confirmMode: settings?.confirm ?? "tap",
    settingsError,
    open,
    end,
    confirmCard: () => cardRef.current?.settle(true),
    declineCard: () => cardRef.current?.settle(false),
    dismissProblem: () => setProblem(null),
    setConfirmMode,
  };
}

export type VoiceAgent = ReturnType<typeof useVoiceAgent>;
