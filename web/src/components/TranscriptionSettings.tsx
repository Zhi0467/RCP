import { AudioLines, LoaderCircle, Pencil, Plug, Unplug } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useState } from "react";
import {
  connectServiceConnection,
  disconnectServiceConnection,
  loadConnectionModels,
  loadServiceConnections,
  loadServiceModels,
  loadVoiceSettings,
  selectDictationService,
  updateServiceConnection,
} from "../api";
import { isDesktopRuntime } from "../desktopRuntime";
import { modelChoices, serviceConnectionFailure, voiceConnectionUpdate } from "../dictation";
import { errorMessage } from "../errors";
import type {
  ServiceConnection,
  ServiceConnectionKind,
  ServiceConnectionPreset,
  ServiceConnectionPurpose,
  ServiceConnections,
  ServiceModels,
  VoiceSettings,
} from "../types";
import { formatServerTimestamp } from "./ServerSettings";

type ServiceChoice = "openai" | "groq" | "gemini" | "custom";

/** Only an OpenAI key can also run the standby voice agent. */
const OPENAI_USES: Record<string, { label: string; purposes: ServiceConnectionPurpose[] }> = {
  dictation: { label: "Dictation", purposes: ["transcription"] },
  voice: { label: "Standby voice agent", purposes: ["voice"] },
  both: { label: "Dictation and standby voice agent", purposes: ["transcription", "voice"] },
};

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

/** What each model does, shown beside it in the card and on each connection. */
const MODEL_ROLES = {
  dictation: { label: "Dictation model", hint: "Turns your speech into text." },
  thinking: { label: "Thinking model", hint: "Does the work you ask the voice agent for." },
  live: { label: "Live voice", hint: "The voice you talk to. RCP sets it." },
};

function serviceChoice(connection: ServiceConnection): ServiceChoice {
  if (connection.kind === "gemini") return "gemini";
  return connection.preset ?? "custom";
}

function openAiUse(purposes: ServiceConnectionPurpose[]): string {
  if (purposes.includes("voice")) return purposes.includes("transcription") ? "both" : "voice";
  return "dictation";
}

function failureText(failure: unknown): string {
  return serviceConnectionFailure(failure) ?? errorMessage(failure);
}

/**
 * Settings card: the signed-in member's own dictation service, standby voice
 * agent connection, and service connections.
 *
 * Unlike the rest of Space settings this belongs to one person. Keys go to the
 * RCP backend once and are never read back.
 */
export function TranscriptionSettings({ writesDisabled = false }: { writesDisabled?: boolean }) {
  const desktop = useMemo(() => isDesktopRuntime(), []);
  const [settings, setSettings] = useState<ServiceConnections | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  // null: closed; "new": Connect; a connection: Edit.
  const [card, setCard] = useState<ServiceConnection | "new" | null>(null);
  const [voice, setVoice] = useState<VoiceSettings | null>(null);
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
  const voiceConnection =
    settings?.connections.find((connection) => connection.purposes.includes("voice")) ?? null;
  return (
    <section className="settings-section transcription-settings">
      <header>
        <span>
          <AudioLines size={16} />
        </span>
        <h2>Dictation and voice</h2>
        <span className="transcription-owner">Only you</span>
      </header>
      <p className="provider-login-intro">Your own services and keys, not shared.</p>
      {settings === null && !error ? <p className="provider-login-intro">Loading…</p> : null}
      {settings ? (
        <>
          <h3 className="transcription-group">Dictation</h3>
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
          <h3 className="transcription-group">Standby voice agent</h3>
          <label className="transcription-picker">
            <span>Runs on</span>
            <select
              value={voiceConnection?.id ?? "off"}
              disabled={disabled}
              onChange={(event) => {
                const update = voiceConnectionUpdate(settings.connections, event.target.value);
                if (update)
                  void run("voice", () =>
                    updateServiceConnection(update.id, { purposes: update.purposes }),
                  );
              }}
            >
              <option value="off">Off</option>
              {/* The agent uses the account, not the connection's transcription model. */}
              {settings.connections
                .filter((connection) => connection.preset === "openai")
                .map((connection, _index, accounts) => (
                  <option key={connection.id} value={connection.id}>
                    {accounts.length > 1
                      ? `${connection.label} account, verified ${formatServerTimestamp(connection.verified_at)}`
                      : `${connection.label} account`}
                  </option>
                ))}
            </select>
          </label>
          {voiceConnection ? (
            <p className="provider-login-detail transcription-note">
              Billed to that OpenAI account at OpenAI&apos;s rate for each minute of talk. Its
              models are on the connection below.
            </p>
          ) : null}
          <h3 className="transcription-group">Your services</h3>
          <div className="provider-login-list">
            {settings.connections.map((connection) => (
              <article key={connection.id} className="provider-login-account">
                <header>
                  <strong>{connection.label}</strong>
                  <div className="service-connection-uses">
                    {settings.dictation === connection.id ? (
                      <span className="provider-path-state ready">Dictation</span>
                    ) : null}
                    {connection.purposes.includes("voice") ? (
                      <span className="provider-path-state ready">Standby voice agent</span>
                    ) : null}
                  </div>
                </header>
                <ConnectionModels connection={connection} voice={voice} />
                {connection.preset === "custom" && connection.base_url ? (
                  <p className="provider-login-detail">
                    <code>{connection.base_url}</code>
                  </p>
                ) : null}
                <p className="provider-login-detail">
                  Verified{" "}
                  <time dateTime={connection.verified_at ?? undefined}>
                    {formatServerTimestamp(connection.verified_at)}
                  </time>
                </p>
                <div className="provider-login-actions">
                  <button
                    className="button secondary compact"
                    type="button"
                    disabled={disabled}
                    onClick={() => setCard(connection)}
                  >
                    <Pencil size={14} /> Edit
                  </button>
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
                onClick={() => setCard("new")}
              >
                <Plug size={14} /> Connect a service
              </button>
            </div>
          </div>
        </>
      ) : null}
      {error ? (
        <div className="settings-error" role="alert">
          {error}
        </div>
      ) : null}
      {card ? (
        <ServiceCard
          editing={card === "new" ? null : card}
          voice={voice}
          onClose={() => setCard(null)}
          onSaved={async () => {
            setCard(null);
            setError(null);
            await refresh();
          }}
        />
      ) : null}
    </section>
  );
}

/** A connection's models with what each one does; only the uses it has. */
function ConnectionModels({
  connection,
  voice,
}: {
  connection: ServiceConnection;
  voice: VoiceSettings | null;
}) {
  const rows: { role: keyof typeof MODEL_ROLES; id: string }[] = [];
  if (connection.purposes.includes("transcription"))
    rows.push({ role: "dictation", id: connection.model });
  if (connection.purposes.includes("voice") && voice)
    rows.push(
      { role: "thinking", id: voice.delegation_model },
      { role: "live", id: voice.live_model },
    );
  if (!rows.length) return <p className="provider-login-detail">Not in use.</p>;
  return (
    <dl className="service-connection-models">
      {rows.map(({ role, id }) => (
        <div key={role}>
          <dt>{MODEL_ROLES[role].label}</dt>
          <dd>
            <code>{id}</code>
            <span>{MODEL_ROLES[role].hint}</span>
          </dd>
        </div>
      ))}
    </dl>
  );
}

// RCP refuses "_" in model ids, so this never collides with a real one.
const OTHER = "__other__";

/**
 * One model: the provider's listed ids plus Other… to type one. Without a list
 * (still loading, or the provider refused) it is a text box and says why.
 */
function ModelField({
  role,
  value,
  onChange,
  listed,
  loading,
  listError,
  disabled,
}: {
  role: keyof typeof MODEL_ROLES;
  value: string;
  onChange: (value: string) => void;
  listed: string[] | null;
  loading: boolean;
  listError: string | null;
  disabled: boolean;
}) {
  const [typing, setTyping] = useState(false);
  const { label, hint } = MODEL_ROLES[role];
  const choices = modelChoices(listed ?? [], value);
  const note = loading
    ? "Loading the provider's models…"
    : listError
      ? `${listError} Type a model id.`
      : listed === null
        ? "Models appear once the key is entered."
        : "";
  return (
    <div className="service-model-field">
      <label>
        {label}
        {listed === null ? (
          <input
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={value}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
          />
        ) : (
          <select
            value={typing ? OTHER : value}
            disabled={disabled}
            onChange={(event) => {
              const next = event.target.value;
              setTyping(next === OTHER);
              if (next !== OTHER) onChange(next);
            }}
          >
            {!value && !typing ? <option value="" disabled /> : null}
            {choices.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
            <option value={OTHER}>Other…</option>
          </select>
        )}
      </label>
      {listed !== null && typing ? (
        <input
          type="text"
          aria-label={`${label} id`}
          autoComplete="off"
          spellCheck={false}
          autoFocus
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : null}
      <p>{[hint, note].filter(Boolean).join(" ")}</p>
    </div>
  );
}

/** Connect a service, or edit a saved one: every model it uses, in one card. */
function ServiceCard({
  editing,
  voice,
  onClose,
  onSaved,
}: {
  editing: ServiceConnection | null;
  voice: VoiceSettings | null;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const titleId = useId();
  const [choice, setChoice] = useState<ServiceChoice>(editing ? serviceChoice(editing) : "openai");
  const [key, setKey] = useState("");
  const [model, setModel] = useState(editing?.model ?? SERVICES.openai.model);
  const [delegation, setDelegation] = useState(voice?.delegation_model ?? "");
  const [use, setUse] = useState(editing ? openAiUse(editing.purposes) : "dictation");
  const [baseUrl, setBaseUrl] = useState(editing?.base_url ?? "");
  const [models, setModels] = useState<ServiceModels | null>(null);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const service = SERVICES[choice];
  const custom = choice === "custom";
  const purposes = choice === "openai" ? OPENAI_USES[use].purposes : ["transcription" as const];
  const dictation = purposes.includes("transcription");
  const voiceUse = purposes.includes("voice");
  const address = editing || (custom ? baseUrl.trim() : key.trim());
  const ready =
    Boolean(address) &&
    (!dictation || Boolean(model.trim())) &&
    (!voiceUse || Boolean(delegation.trim()));
  const destination = custom ? "the server at this address" : service.label;
  const close = () => {
    if (!busy) onClose();
  };

  // A saved connection lists with its stored key; a new one waits for typing to pause.
  useEffect(() => {
    setModels(null);
    setModelsError(null);
    if (!address) return;
    let current = true;
    const timer = window.setTimeout(
      () => {
        setModelsLoading(true);
        const request = editing
          ? loadConnectionModels(editing.id)
          : loadServiceModels({
              kind: service.kind,
              preset: service.preset,
              base_url: custom ? baseUrl.trim() : null,
              key: key.trim(),
            });
        request
          .then((value) => current && setModels(value))
          .catch((failure) => current && setModelsError(failureText(failure)))
          .finally(() => current && setModelsLoading(false));
      },
      editing ? 0 : 600,
    );
    return () => {
      current = false;
      window.clearTimeout(timer);
      setModelsLoading(false);
    };
  }, [editing, address, service.kind, service.preset, custom, baseUrl, key]);

  const save = async () => {
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      const delegationModel = voiceUse ? delegation.trim() : undefined;
      if (editing) {
        await updateServiceConnection(editing.id, {
          purposes,
          model: dictation ? model.trim() : undefined,
          delegation_model: delegationModel,
        });
      } else {
        await connectServiceConnection({
          kind: service.kind,
          preset: service.preset,
          base_url: custom ? baseUrl.trim() : null,
          // The transcription model is unused without dictation; send the preset's own.
          model: dictation ? model.trim() : service.model,
          key: key.trim(),
          purposes,
          delegation_model: delegationModel,
        });
      }
      setKey("");
      await onSaved();
    } catch (failure) {
      setError(failureText(failure));
      setBusy(false);
    }
  };

  const fieldState = {
    loading: modelsLoading,
    listError: modelsError,
    disabled: busy,
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
          void save();
        }}
      >
        <header>
          <h2 id={titleId}>{editing ? `Edit ${editing.label}` : "Connect a service"}</h2>
        </header>
        <div className="transcription-dialog-body">
          {editing ? null : (
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
          )}
          {choice === "openai" ? (
            <label>
              Use for
              <select value={use} disabled={busy} onChange={(event) => setUse(event.target.value)}>
                {Object.entries(OPENAI_USES).map(([value, option]) => (
                  <option key={value} value={value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {custom && !editing ? (
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
          {editing ? (
            <p>
              RCP keeps the key it checked when you connected. To use another key, disconnect and
              connect again.
            </p>
          ) : (
            <>
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
                    Get an API key from {service.label}
                  </a>
                </p>
              ) : null}
            </>
          )}
          {dictation ? (
            <ModelField
              role="dictation"
              value={model}
              onChange={setModel}
              listed={models?.transcription ?? null}
              {...fieldState}
            />
          ) : null}
          {voiceUse ? (
            <>
              <ModelField
                role="thinking"
                value={delegation}
                onChange={setDelegation}
                listed={models?.delegation ?? null}
                {...fieldState}
              />
              {voice ? (
                <div className="service-model-field">
                  <label>
                    {MODEL_ROLES.live.label}
                    <input type="text" value={voice.live_model} readOnly disabled />
                  </label>
                  <p>{MODEL_ROLES.live.hint}</p>
                </div>
              ) : null}
            </>
          ) : null}
          {dictation ? (
            <p>
              Dictation audio goes from your device to RCP, which sends it to {destination}. RCP
              keeps the key on its server, never shows it again, and does not store audio. What{" "}
              {destination} keeps is set by your account there.
            </p>
          ) : null}
          {voiceUse ? (
            <p>
              Standby voice agent audio goes directly between this page and OpenAI; RCP only starts
              each session with the key it keeps on its server.
            </p>
          ) : null}
          <p>
            {dictation
              ? "RCP checks dictation with two short test clips before saving."
              : "RCP checks the key and the thinking model with OpenAI before saving."}
          </p>
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
            {busy ? "Checking…" : editing ? "Save" : "Connect"}
          </button>
        </footer>
      </form>
    </div>
  );
}
