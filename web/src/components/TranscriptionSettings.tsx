import { AudioLines, LoaderCircle, Plug, Unplug } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useState } from "react";
import {
  connectServiceConnection,
  disconnectServiceConnection,
  loadServiceConnections,
  loadVoiceSettings,
  saveVoiceSettings,
  selectDictationService,
  setServiceConnectionPurposes,
} from "../api";
import { isDesktopRuntime } from "../desktopRuntime";
import { serviceConnectionFailure } from "../dictation";
import { errorMessage } from "../errors";
import type {
  ServiceConnectionKind,
  ServiceConnectionPreset,
  ServiceConnectionPurpose,
  ServiceConnections,
  VoiceSettings,
} from "../types";
import { formatServerTimestamp } from "./ServerSettings";

type ServiceChoice = "openai" | "groq" | "gemini" | "custom";

const SERVICES: Record<
  ServiceChoice,
  {
    label: string;
    kind: ServiceConnectionKind;
    preset: ServiceConnectionPreset | null;
    model: string;
    keyPage: string | null;
  }
> = {
  openai: {
    label: "OpenAI",
    kind: "openai_compatible",
    preset: "openai",
    model: "gpt-transcribe",
    keyPage: "https://platform.openai.com/api-keys",
  },
  groq: {
    label: "Groq",
    kind: "openai_compatible",
    preset: "groq",
    model: "whisper-large-v3-turbo",
    keyPage: "https://console.groq.com/keys",
  },
  gemini: {
    label: "Gemini",
    kind: "gemini",
    preset: null,
    model: "gemini-3.5-transcribe",
    keyPage: "https://aistudio.google.com/apikey",
  },
  custom: {
    label: "Custom server",
    kind: "openai_compatible",
    preset: "custom",
    model: "",
    keyPage: null,
  },
};

function failureText(failure: unknown): string {
  return serviceConnectionFailure(failure) ?? errorMessage(failure);
}

/**
 * Settings card: the signed-in member's own dictation service and connections.
 *
 * Unlike the rest of Space settings this belongs to one person. Keys go to the
 * RCP backend once and are never read back.
 */
export function TranscriptionSettings({ writesDisabled = false }: { writesDisabled?: boolean }) {
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const [settings, setSettings] = useState<ServiceConnections | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [voice, setVoice] = useState<VoiceSettings | null>(null);
  const [voiceModel, setVoiceModel] = useState("");
  // A reload never clears an error: a failed action stays reported beside the fresh state.
  const refresh = useCallback(async () => {
    const [connections, voiceSettings] = await Promise.allSettled([
      loadServiceConnections(),
      loadVoiceSettings(),
    ]);
    if (connections.status === "fulfilled") setSettings(connections.value);
    else setError(failureText(connections.reason));
    if (voiceSettings.status === "fulfilled") {
      setVoice(voiceSettings.value);
      setVoiceModel(voiceSettings.value.delegation_model);
    } else if (connections.status === "fulfilled") {
      setError(failureText(voiceSettings.reason));
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function run(kind: string, action: () => Promise<unknown>) {
    setBusy(kind);
    setError(null);
    try {
      await action();
    } catch (failure) {
      setError(failureText(failure));
    } finally {
      await refresh();
      setBusy(null);
    }
  }

  const disabled = writesDisabled || busy !== null;
  return (
    <section className="settings-section transcription-settings">
      <header>
        <span>
          <AudioLines size={16} />
        </span>
        <h2>Transcription</h2>
        <span className="transcription-owner">Only you</span>
      </header>
      <p className="provider-login-intro">
        Your own dictation service and keys. They are not shared with the space: each member
        connects their own and pays for their own use.
      </p>
      {settings === null && !error ? <p className="provider-login-intro">Loading…</p> : null}
      {settings ? (
        <>
          <label className="transcription-picker">
            <span>Dictate with</span>
            <select
              value={settings.dictation}
              disabled={disabled}
              onChange={(event) => {
                const dictation = event.target.value;
                void run("selection", () => selectDictationService(dictation));
              }}
            >
              <option value="system" disabled={!desktop}>
                {desktop ? "macOS dictation" : "macOS dictation (desktop app only)"}
              </option>
              {settings.connections
                .filter((connection) => connection.purposes.includes("transcription"))
                .map((connection) => (
                  <option key={connection.id} value={connection.id}>
                    {connection.label} · {connection.model}
                  </option>
                ))}
            </select>
          </label>
          {!desktop && settings.dictation === "system" ? (
            <p className="provider-login-intro">
              macOS dictation works only in the desktop app. Choose a connection to dictate here.
            </p>
          ) : null}
          <div className="provider-login-list">
            {settings.connections.map((connection) => (
              <article key={connection.id} className="provider-login-account">
                <header>
                  <strong>{connection.label}</strong>
                  <span>{connection.model}</span>
                  {settings.dictation === connection.id ? (
                    <span className="provider-path-state ready">In use</span>
                  ) : null}
                </header>
                {connection.preset === "custom" && connection.base_url ? (
                  <p className="provider-login-detail">
                    <code>{connection.base_url}</code>
                  </p>
                ) : null}
                <p className="provider-login-detail">
                  Accepts {connection.formats.join(", ")}. Last verified{" "}
                  <time dateTime={connection.verified_at ?? undefined}>
                    {formatServerTimestamp(connection.verified_at)}
                  </time>
                  .
                </p>
                <div className="provider-login-actions">
                  {connection.preset === "openai" ? (
                    <label className="transcription-voice-toggle">
                      <input
                        type="checkbox"
                        checked={connection.purposes.includes("voice")}
                        disabled={disabled}
                        onChange={(event) => {
                          const purposes: ServiceConnectionPurpose[] = connection.purposes.filter(
                            (item) => item !== "voice",
                          );
                          if (event.target.checked) purposes.push("voice");
                          void run(`voice:${connection.id}`, () =>
                            setServiceConnectionPurposes(connection.id, purposes),
                          );
                        }}
                      />
                      Use for voice
                    </label>
                  ) : null}
                  <button
                    className="button secondary compact"
                    type="button"
                    disabled={disabled}
                    onClick={() =>
                      void run(connection.id, () => disconnectServiceConnection(connection.id))
                    }
                  >
                    {busy === connection.id ? (
                      <LoaderCircle size={14} className="spin" />
                    ) : (
                      <Unplug size={14} />
                    )}
                    Disconnect
                  </button>
                </div>
              </article>
            ))}
            <div className="provider-login-actions">
              <button
                className="button compact"
                type="button"
                disabled={disabled}
                onClick={() => setConnecting(true)}
              >
                <Plug size={14} /> Connect a service
              </button>
            </div>
          </div>
          {voice ? (
            <form
              className="transcription-picker"
              onSubmit={(event) => {
                event.preventDefault();
                const model = voiceModel.trim();
                if (model)
                  void run("voice-model", () => saveVoiceSettings({ delegation_model: model }));
              }}
            >
              <span>Voice delegation model</span>
              <div className="provider-login-token">
                <input
                  type="text"
                  autoComplete="off"
                  spellCheck={false}
                  value={voiceModel}
                  disabled={disabled}
                  onChange={(event) => setVoiceModel(event.target.value)}
                />
                <button
                  className="button secondary compact"
                  type="submit"
                  disabled={
                    disabled || !voiceModel.trim() || voiceModel.trim() === voice.delegation_model
                  }
                >
                  Save
                </button>
              </div>
              <p className="provider-login-detail">
                Voice runs on the connection marked Use for voice, at that account&apos;s cost.
              </p>
            </form>
          ) : null}
        </>
      ) : null}
      {error ? (
        <div className="settings-error" role="alert">
          {error}
        </div>
      ) : null}
      {connecting ? (
        <ConnectServiceDialog
          onClose={() => setConnecting(false)}
          onConnected={async () => {
            setConnecting(false);
            setError(null);
            await refresh();
          }}
        />
      ) : null}
    </section>
  );
}

function ConnectServiceDialog({
  onClose,
  onConnected,
}: {
  onClose: () => void;
  onConnected: () => Promise<void>;
}) {
  const titleId = useId();
  const [choice, setChoice] = useState<ServiceChoice>("openai");
  const [key, setKey] = useState("");
  const [model, setModel] = useState(SERVICES.openai.model);
  const [baseUrl, setBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const service = SERVICES[choice];
  const custom = choice === "custom";
  const ready = Boolean(model.trim()) && (custom ? Boolean(baseUrl.trim()) : Boolean(key.trim()));
  const destination = custom ? "the server at this address" : service.label;
  const close = () => {
    if (!busy) onClose();
  };

  const connect = async () => {
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      await connectServiceConnection({
        kind: service.kind,
        preset: service.preset,
        base_url: custom ? baseUrl.trim() : null,
        model: model.trim(),
        key: key.trim(),
        purposes: ["transcription"],
      });
      setKey("");
      await onConnected();
    } catch (failure) {
      setError(failureText(failure));
      setBusy(false);
    }
  };

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) close();
      }}
    >
      <form
        className="transcription-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={(event) => {
          if (event.key === "Escape") close();
        }}
        onSubmit={(event) => {
          event.preventDefault();
          void connect();
        }}
      >
        <header>
          <h2 id={titleId}>Connect a transcription service</h2>
        </header>
        <div className="transcription-dialog-body">
          <label>
            Service
            <select
              autoFocus
              value={choice}
              disabled={busy}
              onChange={(event) => {
                const next = event.target.value as ServiceChoice;
                setChoice(next);
                setModel(SERVICES[next].model);
                setError(null);
              }}
            >
              {(Object.keys(SERVICES) as ServiceChoice[]).map((value) => (
                <option key={value} value={value}>
                  {SERVICES[value].label}
                </option>
              ))}
            </select>
          </label>
          {custom ? (
            <label>
              Base URL
              <input
                type="url"
                inputMode="url"
                autoComplete="off"
                placeholder="https://transcribe.example/v1"
                value={baseUrl}
                disabled={busy}
                onChange={(event) => setBaseUrl(event.target.value)}
              />
            </label>
          ) : null}
          <label>
            {custom ? "API key (optional)" : "API key"}
            <input
              type="password"
              autoComplete="off"
              value={key}
              disabled={busy}
              onChange={(event) => setKey(event.target.value)}
            />
          </label>
          {service.keyPage ? (
            <p>
              <a href={service.keyPage} target="_blank" rel="noreferrer">
                Create a {service.label} API key
              </a>
            </p>
          ) : null}
          <label>
            Model
            <input
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={model}
              disabled={busy}
              onChange={(event) => setModel(event.target.value)}
            />
          </label>
          <p>
            Dictation audio goes from your device to RCP, which sends it to {destination}. RCP keeps
            the key on its server, never shows it again, and does not store audio. What{" "}
            {destination} keeps is set by your account there.
          </p>
          <p>RCP checks the connection with two short test clips before saving it.</p>
        </div>
        {error ? (
          <div className="transcription-dialog-error" role="alert">
            {error}
          </div>
        ) : null}
        <footer>
          <button className="button secondary" type="button" disabled={busy} onClick={close}>
            Cancel
          </button>
          <button className="button primary" type="submit" disabled={busy || !ready}>
            {busy ? <LoaderCircle className="spin" size={14} /> : <Plug size={14} />}
            {busy ? "Checking…" : "Connect"}
          </button>
        </footer>
      </form>
    </div>
  );
}
