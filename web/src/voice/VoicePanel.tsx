import { AudioLines, Check, PhoneOff, Settings, X } from "lucide-react";
import { useEffect, useState } from "react";
import { DraggableWindow } from "../ui/DraggableWindow";
import { VOICE_PANEL_SIZE_STORAGE_KEY } from "../ui/floatingWindow";
import type { VoiceAgent } from "./useVoiceAgent";
import type { VoicePin } from "./voiceExecutor";
import { loadGraphRefs } from "../core/api";
import { branchName } from "../core/branchNames";
import type { GraphRef } from "../core/types";

function elapsedText(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <time className="voice-elapsed">
      {elapsedText(Math.max(0, Math.floor((now - since) / 1000)))}
    </time>
  );
}

const CARD_TITLES: Record<string, string> = {
  rcp_send_conversation_message: "Send a Work message",
  rcp_start_experiment: "Start an Experiment episode",
  rcp_authorize_auto_research: "Authorize Auto-research",
  rcp_run_terminal_command: "Run a terminal command",
  rcp_thread_send: "Send to the agent",
};

function valueText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function ConfirmationCard({
  pin,
  voice,
  graphRefs,
}: {
  pin: VoicePin;
  voice: VoiceAgent;
  graphRefs: VoicePanelProps["graphRefs"];
}) {
  const branchId = pin.graph_target.kind === "branch" ? pin.graph_target.branch_id : null;
  const openProjectRefs = graphRefs?.projectId === pin.project_id ? graphRefs.refs : null;
  // A card for another project reads that project's own branch names.
  const [pinnedRefs, setPinnedRefs] = useState<{ projectId: string; refs: GraphRef[] } | null>(
    null,
  );
  useEffect(() => {
    if (!branchId || openProjectRefs) return;
    let current = true;
    loadGraphRefs(pin.project_id).then(
      (refs) => current && setPinnedRefs({ projectId: pin.project_id, refs }),
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, [branchId, openProjectRefs, pin.project_id]);
  const refs =
    openProjectRefs ?? (pinnedRefs?.projectId === pin.project_id ? pinnedRefs.refs : null);
  const target = branchId ? branchName(refs, { kind: "branch", branch_id: branchId }) : "main";
  const profile = pin.provider_profile;
  const terminal = pin.terminal;
  const rows: Array<[string, string]> = [
    ...(pin.thread ? ([["Agent", pin.thread.agent_name]] as Array<[string, string]>) : []),
    ["Project", pin.project_name],
    ...(terminal
      ? ([
          ["Repository", terminal.repository_id],
          ["Machine", terminal.machine_id],
        ] as Array<[string, string]>)
      : ([["Graph", target]] as Array<[string, string]>)),
    ...(pin.mode ? ([["Mode", pin.mode]] as Array<[string, string]>) : []),
    ...(profile
      ? ([
          [
            "Provider",
            [profile.provider, profile.model || "default model", profile.reasoning, profile.run_on]
              .filter(Boolean)
              .join(" · "),
          ],
        ] as Array<[string, string]>)
      : []),
    ...(pin.budget !== null
      ? ([["Budget", `${pin.budget} invocations`]] as Array<[string, string]>)
      : []),
    ...(pin.truth_scope
      ? ([["Truth scope", pin.truth_scope.join(", ") || "None"]] as Array<[string, string]>)
      : []),
  ];
  const shown = terminal
    ? []
    : Object.entries(pin.arguments).filter(
        ([key]) => !["invocation_ceiling", "mode", "starting_instruction"].includes(key),
      );
  return (
    <section
      className="voice-card"
      role="alertdialog"
      aria-label={CARD_TITLES[pin.tool] ?? pin.tool}
    >
      <h3>{CARD_TITLES[pin.tool] ?? pin.tool}</h3>
      <dl>
        {rows.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
        {shown.map(([key, value]) => (
          <div key={key}>
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd>{valueText(value)}</dd>
          </div>
        ))}
      </dl>
      {pin.starting_instruction ? (
        <div className="voice-card-instruction">
          <span>Starting instruction</span>
          <p>{pin.starting_instruction}</p>
        </div>
      ) : null}
      {terminal ? (
        <div className="voice-card-instruction">
          <span>Command</span>
          <code>{terminal.command}</code>
        </div>
      ) : null}
      <footer>
        <button className="button secondary compact" type="button" onClick={voice.declineCard}>
          <X size={14} /> Decline
        </button>
        <button className="button primary compact" type="button" onClick={voice.confirmCard}>
          <Check size={14} /> Confirm
        </button>
      </footer>
    </section>
  );
}

type VoicePanelProps = {
  voice: VoiceAgent;
  onOpenSettings: () => void;
  /** The open project's refs, so a confirmation names its branch. */
  graphRefs?: { projectId: string; refs: GraphRef[] | null } | null;
};

/** The floating voice panel; it shows while a session runs or after one failed.
 * It is a window the member can drag and resize so it never pins over the page. */
export function VoicePanel(props: VoicePanelProps) {
  const { voice } = props;
  if (voice.phase === "idle" && !voice.problem) return null;
  return (
    <DraggableWindow
      className="voice-window"
      kind="voice"
      resizable
      sizeStorageKey={VOICE_PANEL_SIZE_STORAGE_KEY}
    >
      <VoicePanelContent {...props} />
    </DraggableWindow>
  );
}

function VoicePanelContent({ voice, onOpenSettings, graphRefs = null }: VoicePanelProps) {
  if (voice.phase === "idle") {
    if (!voice.problem) return null;
    const notConnected = voice.problem.code === "voice_not_connected";
    return (
      <aside className="voice-panel voice-panel-problem" role="status" data-drag-handle>
        <p>
          {notConnected
            ? "The standby voice agent needs your own OpenAI connection. Choose one in Settings, under Standby voice agent, Runs on."
            : voice.problem.text}
        </p>
        <div className="voice-panel-actions">
          {notConnected ? (
            <button
              className="button compact"
              type="button"
              onClick={() => {
                voice.dismissProblem();
                onOpenSettings();
              }}
            >
              <Settings size={14} /> Open Settings
            </button>
          ) : null}
          <button
            className="icon-button"
            type="button"
            aria-label="Dismiss"
            onClick={voice.dismissProblem}
          >
            <X size={14} />
          </button>
        </div>
      </aside>
    );
  }
  return (
    <aside className="voice-panel" aria-label="Voice" data-drag-handle>
      <header>
        <AudioLines size={16} aria-hidden="true" />
        <strong>{voice.thread?.agent_name ?? "Voice"}</strong>
        {voice.startedAt !== null ? <Elapsed since={voice.startedAt} /> : <span>Connecting…</span>}
        <button className="button secondary compact" type="button" onClick={() => voice.end()}>
          <PhoneOff size={14} /> End
        </button>
      </header>
      <div className="voice-confirm-mode" role="radiogroup" aria-label="Confirmation">
        {(
          [
            ["tap", "Tap to confirm"],
            ["none", "Run without confirming"],
          ] as const
        ).map(([mode, label]) => (
          <label key={mode}>
            <input
              type="radio"
              name="voice-confirm-mode"
              checked={voice.confirmMode === mode}
              onChange={() => void voice.setConfirmMode(mode)}
            />
            {label}
          </label>
        ))}
      </div>
      {voice.problem ? (
        <p className="voice-panel-error" role="status">
          {voice.problem.text}
        </p>
      ) : null}
      {voice.settingsError ? (
        <p className="voice-panel-error" role="alert">
          {voice.settingsError}
        </p>
      ) : null}
      {voice.card ? (
        <ConfirmationCard pin={voice.card} voice={voice} graphRefs={graphRefs} />
      ) : null}
      <ol className="voice-transcript" aria-live="polite" data-text-selectable>
        {voice.transcript.map((line, index) => (
          <li key={index} data-role={line.role}>
            {line.text}
          </li>
        ))}
      </ol>
    </aside>
  );
}
