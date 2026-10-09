import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import {
  api,
  ApiError,
  loadEpisodes,
  loadClientRequest,
  loadVoiceSettings,
  createVoiceSession,
  loadVoiceSessions,
  loadVoiceGeneration,
  saveVoiceSession,
  deleteVoiceSession,
  registerAccessLossHandler,
  saveVoiceSettings,
} from "../core/api";
import { desktopKeepsVoiceWhileHidden } from "../core/desktopRuntime";
import { serviceConnectionFailure } from "./dictation";
import { errorMessage } from "../core/errors";
import { MicrophoneBusyError } from "./microphone";
import { isControlNode } from "../graph/researchType";
import { catalog, catalogAsFunctionTools, resolve, type CatalogTool } from "./toolCatalog";
import type {
  AgentProfile,
  AgentTask,
  ProjectSnapshot,
  VoiceSettings,
  VoiceSavedSession,
  VoiceSessionMetadata,
  VoiceSessionResponse,
} from "../core/types";
import {
  createIdentityGate,
  createVoiceSaveQueue,
  appendVoiceTranscript,
  boundVoiceTranscript,
  createVoiceSourceLabels,
  createFinishedResultOffer,
  voiceWatchFromReceipt,
  reconcileVoiceReceipts,
  type VoiceReceiptTarget,
  createVoiceExecutor,
  episodeWatchStatus,
  taskWatchStatus,
  voiceCallOutcome,
  voiceCommentary,
  type VoiceConfirmMode,
  type VoiceIdentityGate,
  type VoicePin,
  type VoiceWatch,
  type VoiceWatchStatus,
} from "./voiceExecutor";
import {
  endOnPageSuspend,
  openVoiceSession,
  type VoiceEndReason,
  type VoiceSession,
} from "./voiceSession";
import { assertVoiceMayType, terminalCommandInput, terminalRepository } from "./voiceTerminal";
import { conversationSendTarget, type WebMcpConversationSource } from "../webmcp/index";

const VOICE_WATCH_POLL_MS = 10_000;
// Speech deltas batch into one saved snapshot; receipts and End save at once.
const VOICE_TRANSCRIPT_SAVE_MS = 2_000;
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

/** Spoken lines, plus one activity line per tool call so the member sees what ran. */
export type VoiceTranscriptLine = {
  role: "member" | "agent" | "tool";
  text: string;
  callId?: string;
};

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
    terminal: null,
  };
  if (name === "rcp_run_terminal_command") {
    const input = terminalCommandInput(args);
    // Read at show and Confirm time, so a vanished or changed repository refuses.
    const repository = await terminalRepository(project.id, input.repository_id, api);
    // A terminal voice may not type into is refused before the card, not after Confirm.
    await assertVoiceMayType(project.id, input.repository_id, api);
    return {
      ...base,
      arguments: { ...input },
      terminal: {
        repository_id: repository.repository_id,
        machine_id: repository.machine_id,
        containment: repository.containment,
        command: input.command,
      },
    };
  }
  if (name === "rcp_start_experiment") {
    const id = String(args.experiment_id ?? "");
    const node = project.graph.nodes[id];
    if (!node || !isControlNode(node.type)) throw new Error(`${id} is not an Experiment here.`);
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
    // A Send that would be refused is refused now, not after the member taps Confirm.
    if (target.refusal) throw new Error(target.refusal);
    return {
      ...base,
      arguments: { ...args },
      mode: typeof args.mode === "string" ? args.mode : null,
      provider_profile: { ...target.config, conversation: target.surface },
      // The write roots a Work turn will get, so a scope change invalidates the card.
      truth_scope: target.runTruthScope,
    };
  }
  throw new Error(`${name} has no confirmation card.`);
}

function toolActivity(name: string, outcome: ReturnType<typeof voiceCallOutcome> | null): string {
  const label = name.replace(/^rcp_/, "").replaceAll("_", " ");
  if (!outcome) return `${label}…`;
  if (outcome.ok) return `${label}: done`;
  if (outcome.code === "not_confirmed") return `${label}: not confirmed`;
  return `${label}: refused${outcome.error ? ` (${outcome.error})` : ""}`;
}

function setToolLine(
  lines: VoiceTranscriptLine[],
  callId: string,
  text: string,
): VoiceTranscriptLine[] {
  const index = lines.findIndex((line) => line.callId === callId);
  if (index < 0)
    return [...lines, { role: "tool" as const, text, callId }].slice(-VOICE_TRANSCRIPT_LINES);
  return lines.map((line, i) => (i === index ? { ...line, text } : line));
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
  hidden:
    "Voice ended when the window was hidden or suspended. Only the desktop app on macOS 14 or later keeps it open while hidden.",
  hard_cap: "Voice ended at its time limit.",
  connection: "Voice ended: the connection dropped.",
  upstream: "Voice ended by OpenAI.",
};

function openFailure(failure: unknown): VoiceProblem {
  if (failure instanceof MicrophoneBusyError)
    return { code: "microphone_busy", text: failure.message };
  let code: string | null = null;
  if (failure instanceof ApiError) code = failure.code ?? null;
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
  const [historyOpen, setHistoryOpen] = useState(false);
  const [recentSessions, setRecentSessions] = useState<VoiceSessionMetadata[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const scopeRef = useRef({ spaceId, ready, epoch: 0 });
  if (scopeRef.current.spaceId !== spaceId || scopeRef.current.ready !== ready) {
    scopeRef.current = { spaceId, ready, epoch: scopeRef.current.epoch + 1 };
  }
  const ownerRef = useRef<object | null>(null);
  const checkGenerationRef = useRef<(() => Promise<void>) | null>(null);
  const resumeSummaryRef = useRef(false);
  const watchTargetsRef = useRef(new Map<string, VoiceReceiptTarget>());
  const offerRef = useRef<ReturnType<typeof createFinishedResultOffer> | null>(null);
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
  // Unknown counts as not kept: an older desktop shell or a browser ends on hiding.
  const [keepWhileHidden, setKeepWhileHidden] = useState(false);
  useEffect(() => {
    void desktopKeepsVoiceWhileHidden()
      .then(setKeepWhileHidden)
      .catch(() => setKeepWhileHidden(false));
  }, []);
  // Confirm-mode writes in click order; `latest` fences every older settings result.
  const confirmSaves = useRef<{ chain: Promise<unknown>; latest: number }>({
    chain: Promise.resolve(),
    latest: 0,
  });

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

  const currentTarget = useCallback((): VoiceReceiptTarget | null => {
    const project = page.current.project;
    return project
      ? {
          project_id: project.id,
          project_name: project.name,
          graph_target: {
            kind: project.graph_target?.kind ?? "main",
            branch_id:
              project.graph_target?.kind === "branch" ? project.graph_target.branch_id : null,
          },
        }
      : null;
  }, [page]);

  const refreshHistory = useCallback(async (offset = 0) => {
    const scope = scopeRef.current;
    setHistoryError(null);
    try {
      const result = await loadVoiceSessions(offset);
      if (!scopeRef.current.ready || scopeRef.current.epoch !== scope.epoch) return;
      setRecentSessions((previous) =>
        offset ? [...previous, ...result.sessions] : result.sessions,
      );
      setNextOffset(result.next_offset);
    } catch (failure) {
      if (scopeRef.current.epoch === scope.epoch) setHistoryError(errorMessage(failure));
    }
  }, []);

  const open = useCallback(
    async (resumeId?: string) => {
      if (sessionRef.current || startingRef.current || !readyRef.current) return;
      startingRef.current = true;
      const gate = createIdentityGate();
      gateRef.current = gate;
      const owner = {};
      ownerRef.current = owner;
      const capturedEpoch = scopeRef.current.epoch;
      const sameOwner = () =>
        ownerRef.current === owner &&
        scopeRef.current.ready &&
        scopeRef.current.epoch === capturedEpoch;
      // A retired session still finishes its own saves (its End snapshot among
      // them) while the member's identity scope holds; a new owner never blocks it.
      const sameScope = () => scopeRef.current.ready && scopeRef.current.epoch === capturedEpoch;
      let record: VoiceSavedSession | null = null;
      let bounds: VoiceSessionResponse["limits"] | null = null;
      let endedNotice: string | null = null;
      const saves = createVoiceSaveQueue<VoiceSavedSession>(async (snapshot) => {
        // Revision allocation is serialized with the request, not with incoming deltas.
        snapshot.revision = (record?.revision ?? snapshot.revision) + 1;
        if (record) record.revision = snapshot.revision;
        await saveVoiceSession(snapshot);
      }, sameScope);
      const fenceFailure = async (failure: unknown) => {
        if (!sameOwner()) return;
        if (!(failure instanceof ApiError) || ![403, 404, 409].includes(failure.status)) return;
        endedNotice =
          failure.code === "voice_session_superseded"
            ? "Voice ended because this conversation was resumed on another device."
            : "Voice ended because this saved conversation is no longer available.";
        if (sessionRef.current) await sessionRef.current.end("member", { immediate: true });
        else gate.lose();
      };
      let transcriptTimer: number | null = null;
      const save = async () => {
        if (transcriptTimer !== null) window.clearTimeout(transcriptTimer);
        transcriptTimer = null;
        if (!record || !bounds) return;
        try {
          record = boundVoiceTranscript(record, {
            entry_bytes: bounds.transcript_entry_max_bytes,
            session_bytes: bounds.transcript_session_max_bytes,
            max_entries: bounds.transcript_max_entries,
          });
          await saves.save(structuredClone(record));
        } catch (failure) {
          if (!sameOwner()) {
            gate.lose();
            throw failure;
          }
          await fenceFailure(failure);
          setProblem({
            code: failure instanceof ApiError ? (failure.code ?? null) : null,
            text:
              endedNotice ??
              "Voice history could not be saved. Resume uses the last saved transcript.",
          });
          throw failure;
        }
      };
      checkGenerationRef.current = async () => {
        if (!record || !sameOwner() || !gate.ok()) return;
        try {
          const current = await loadVoiceGeneration(record.id);
          if (current.generation !== record.generation)
            throw new ApiError("Voice session superseded.", 409, {
              code: "voice_session_superseded",
            });
        } catch (failure) {
          if (gate.ok()) await fenceFailure(failure);
        }
      };
      watchesRef.current = [];
      watchTargetsRef.current.clear();
      resumeSummaryRef.current = Boolean(resumeId);
      offerRef.current = createFinishedResultOffer({
        currentTarget: () => (gate.ok() ? currentTarget() : null),
        list: async (watch) => {
          const tool = resolve("rcp_list_artifacts");
          if (!tool.ok) throw new Error(tool.refusal);
          const result = await tool.definition.execute({
            [watch.record === "task" ? "task_id" : "episode_id"]: watch.id,
          });
          const parsed = JSON.parse(result.content.map((item) => item.text).join("\n"));
          if (!Array.isArray(parsed.artifacts)) throw new Error("The result could not be listed.");
          return parsed.artifacts;
        },
        open: async (viewerId) => {
          const tool = resolve("rcp_open_artifact");
          if (!tool.ok) throw new Error(tool.refusal);
          const result = await tool.definition.execute({ viewer_id: viewerId });
          const outcome = voiceCallOutcome(result.content.map((item) => item.text).join("\n"));
          if (!outcome.ok) throw new Error(outcome.error ?? "The artifact could not be opened.");
        },
      });
      const finishedTool: CatalogTool = {
        name: "rcp_open_finished_result",
        description:
          "Only after the member says yes to the most recent finished-work offer, resolve and open that result. A previous session or historical yes never authorizes opening.",
        inputSchema: { type: "object", properties: {}, additionalProperties: false },
        confirm: () => false,
        alwaysConfirm: false,
      };
      const resolveVoice: typeof resolve = (name) =>
        name === finishedTool.name
          ? {
              ok: true,
              definition: {
                ...finishedTool,
                annotations: { readOnlyHint: true },
                execute: async (args) => {
                  if (Object.keys(args).length) throw new Error("This tool takes no arguments.");
                  const opened = await offerRef.current?.open();
                  return { content: [{ type: "text", text: JSON.stringify({ opened }) }] };
                },
              },
            }
          : resolve(name);
      setHistoryOpen(false);
      setProblem(null);
      setTranscript([]);
      setPhase("starting");
      settingsRef.current = null;
      setSettings(null);
      setSettingsError(null);
      const loadSeq = confirmSaves.current.latest;
      void loadVoiceSettings()
        .then((loaded) => {
          if (gateRef.current !== gate || !gate.ok() || confirmSaves.current.latest !== loadSeq)
            return;
          settingsRef.current = loaded;
          setSettings(loaded);
        })
        .catch((failure) => {
          if (gateRef.current === gate) setSettingsError(errorMessage(failure));
        });
      const sourceLabels = createVoiceSourceLabels();
      let executor: ReturnType<typeof createVoiceExecutor>;
      let session: VoiceSession;
      try {
        session = await openVoiceSession(
          [
            ...catalogAsFunctionTools(),
            {
              type: "function",
              name: finishedTool.name,
              description: finishedTool.description,
              parameters: finishedTool.inputSchema,
            },
          ],
          {
            onTranscript: (role, delta, order) => {
              if (!delta) return;
              offerRef.current?.speech(role, order);
              const source = sourceLabels.speech(role, order);
              setTranscript((lines) => appendTranscript(lines, role, delta));
              if (record && bounds) {
                record.entries = appendVoiceTranscript(record.entries, role, delta, order, source);
                transcriptTimer ??= window.setTimeout(
                  () => void save().catch(() => {}),
                  VOICE_TRANSCRIPT_SAVE_MS,
                );
              }
            },
            onFunctionCall: (call) => {
              sourceLabels.capture(call.call_id, call.name, call.arguments, currentTarget());
              setTranscript((lines) =>
                setToolLine(lines, call.call_id, toolActivity(call.name, null)),
              );
              void executor.run(call).then((output) => {
                if (output === null) return;
                sourceLabels.discard(call.call_id);
                if (!gate.ok()) return;
                setTranscript((lines) =>
                  setToolLine(
                    lines,
                    call.call_id,
                    toolActivity(call.name, voiceCallOutcome(output)),
                  ),
                );
                sessionRef.current?.sendFunctionOutput(call.call_id, output);
              });
            },
            onEnded: (reason) => {
              if (record && !endedNotice) {
                record.ended = true;
                void save().catch(() => {});
              }
              if (gateRef.current === gate) gateRef.current = null;
              gate.lose();
              sessionRef.current = null;
              cardRef.current?.settle(false);
              watchesRef.current = [];
              offerRef.current = null;
              setPhase("idle");
              setStartedAt(null);
              setTranscript([]);
              const notice = endedNotice ?? END_NOTICES[reason];
              setProblem(notice ? { code: null, text: notice } : null);
            },
          },
          gate,
          {
            requestSession: async (body, signal) => {
              const answer = await createVoiceSession(
                { ...body, ...(resumeId ? { resume_id: resumeId } : {}) },
                signal,
              );
              if (!sameOwner() || !gate.ok()) throw new Error("Voice identity changed.");
              record = answer.session;
              bounds = answer.limits;
              if (answer.input_truncated)
                setProblem({
                  code: null,
                  text: "Resume includes the newest part of this conversation; older text was truncated.",
                });
              setTranscript(
                record.entries
                  .slice(-VOICE_TRANSCRIPT_LINES)
                  .map((entry) => ({ role: entry.speaker, text: entry.text })),
              );
              if (resumeId) {
                record.receipts = await reconcileVoiceReceipts(record.receipts, loadClientRequest);
                if (!sameOwner() || !gate.ok()) throw new Error("Voice identity changed.");
                await save();
              }
              for (const receipt of record.receipts) {
                const watch = voiceWatchFromReceipt(receipt);
                if (
                  watch &&
                  !watchesRef.current.some(
                    (item) => item.id === watch.id && item.project_id === watch.project_id,
                  )
                ) {
                  watchesRef.current.push(watch);
                  watchTargetsRef.current.set(watch.id, receipt.target);
                }
              }
              executor = createVoiceExecutor({
                gate,
                catalog: () => [...catalog(), finishedTool],
                resolve: resolveVoice,
                confirmMode: (): VoiceConfirmMode => settingsRef.current?.confirm ?? "tap",
                pin: (name, args) => buildVoicePin(name, args, page.current),
                requestConfirmation,
                initialReceipts: record.receipts,
                target: currentTarget,
                onSucceeded: (_name, _args, _output, callId) => sourceLabels.succeeded(callId),
                saveReceipt: async (receipt) => {
                  if (!record || !bounds) throw new Error("Voice history is unavailable.");
                  const index = record.receipts.findIndex(
                    (item) =>
                      item.call_id === receipt.call_id ||
                      (Boolean(receipt.request_id) && item.request_id === receipt.request_id),
                  );
                  if (index < 0) {
                    if (record.receipts.length >= bounds.transcript_max_receipts)
                      throw new Error(
                        "This voice conversation has reached its action limit. Start a new conversation.",
                      );
                    record.receipts.push(receipt);
                  } else record.receipts[index] = receipt;
                  await save();
                  const watch = voiceWatchFromReceipt(receipt);
                  if (watch && gate.ok()) {
                    if (
                      !watchesRef.current.some(
                        (item) => item.id === watch.id && item.project_id === watch.project_id,
                      )
                    )
                      watchesRef.current.push(watch);
                    watchTargetsRef.current.set(watch.id, receipt.target);
                  }
                },
              });
              return answer;
            },
          },
        );
      } catch (failure) {
        if (gateRef.current === gate) gateRef.current = null;
        setPhase("idle");
        setProblem(gate.ok() ? openFailure(failure) : null);
        return;
      } finally {
        startingRef.current = false;
      }
      if (!readyRef.current) gate.lose();
      if (!gate.ok()) return;
      sessionRef.current = session;
      setStartedAt(Date.now());
      setPhase("open");
    },
    [page, requestConfirmation, currentTarget],
  );

  const setConfirmMode = useCallback(async (confirm: VoiceConfirmMode) => {
    // Saves run in click order, and only the newest click's result applies; a response
    // that lands after this session ended, or another member's opened, is dropped.
    const gate = gateRef.current;
    const saves = confirmSaves.current;
    const seq = ++saves.latest;
    const sameMember = () => gate !== null && gateRef.current === gate && gate.ok();
    const live = () => sameMember() && saves.latest === seq;
    setSettingsError(null);
    try {
      const current = settingsRef.current ?? (await loadVoiceSettings());
      if (!live()) return;
      // The choice shows at once; Tap also applies at once, Run without confirming once saved.
      setSettings({ ...current, confirm });
      if (confirm === "tap") settingsRef.current = { ...current, confirm };
      // A queued save rechecks the member: the PUT rides whoever's cookie is current.
      const save = saves.chain.then(() => {
        if (!sameMember()) throw new Error("Voice ended before this choice was saved.");
        return saveVoiceSettings({ confirm });
      });
      saves.chain = save.catch(() => {});
      const saved = await save;
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
    if (!ready) {
      setHistoryOpen(false);
      setRecentSessions([]);
      setNextOffset(null);
      setHistoryError(null);
      ownerRef.current = null;
      gateRef.current?.lose();
    }
  }, [ready]);
  useEffect(() => {
    setHistoryOpen(false);
    setRecentSessions([]);
    return () => {
      ownerRef.current = null;
      end("space", true);
    };
  }, [end, spaceId]);
  useEffect(() => {
    if (!active) return;
    registerAccessLossHandler(() => {
      ownerRef.current = null;
      gateRef.current?.lose();
    });
    const stopSuspend = endOnPageSuspend(() => end("hidden", true), { keepWhileHidden });
    return () => {
      registerAccessLossHandler(null);
      stopSuspend();
    };
  }, [active, end, keepWhileHidden]);

  // Speak first only about this session's own starts, wherever the member has gone.
  useEffect(() => {
    if (phase !== "open") return;
    let polling = false;
    const poll = async () => {
      const session = sessionRef.current;
      if (polling || !session) return;
      polling = true;
      try {
        const summarizing = resumeSummaryRef.current;
        let finished = 0,
          running = 0,
          needsYou = 0,
          unavailable = 0;
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
                unavailable += 1;
                drop();
                continue;
              }
              status = episodeWatchStatus(episode);
            }
          } catch (failure) {
            unavailable += 1;
            if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) drop();
            continue;
          }
          if (sessionRef.current !== session) return;
          if (status === "finished") finished += 1;
          else if (status === "needs_you") needsYou += 1;
          else running += 1;
          if (status !== watch.last) {
            // A return to running clears needs-you, so the next request is announced too.
            watch.last = status;
            if (status && !summarizing) {
              const offer = status === "finished" ? " Would you like me to open the result?" : "";
              if (status === "finished") {
                const target = watchTargetsRef.current.get(watch.id);
                if (target) offerRef.current?.offer(watch, target);
              }
              session.speak(
                voiceCommentary(
                  watch.kind,
                  watch.project_name,
                  status,
                  session.limits.commentary_max_chars - offer.length,
                ) + offer,
              );
            }
          }
          if (status === "finished") drop();
        }
        if (summarizing && sessionRef.current === session) {
          resumeSummaryRef.current = false;
          session.speak(
            `Earlier work: ${finished} finished, ${running} still running, ${needsYou} need you, ${unavailable} could not be checked.`,
          );
        }
        // Check quiet sessions without rewriting their unchanged transcript.
        if (sessionRef.current === session) await checkGenerationRef.current?.();
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), VOICE_WATCH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [phase]);

  return {
    phase,
    problem,
    startedAt,
    transcript,
    card,
    historyOpen,
    recentSessions,
    nextOffset,
    historyError,
    toggleHistory: () => {
      setHistoryOpen((shown) => !shown);
      void refreshHistory();
    },
    closeHistory: () => setHistoryOpen(false),
    moreHistory: () => nextOffset !== null && void refreshHistory(nextOffset),
    deleteSession: async (id: string) => {
      const epoch = scopeRef.current.epoch;
      try {
        await deleteVoiceSession(id);
        if (scopeRef.current.epoch === epoch) await refreshHistory();
      } catch (failure) {
        if (scopeRef.current.epoch === epoch) setHistoryError(errorMessage(failure));
      }
    },
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
