import { useState } from "react";
import { updateSpaceMachine } from "../core/api";
import { errorMessage } from "../core/errors";
import type { AutocompactProvider, SpaceMachine } from "../core/types";

/** One provider's auto-compact setting for every turn on this machine, read at each launch. */
export function MachineAutocompactRow({
  record,
  option,
  writesDisabled,
  onRecordChange,
}: {
  record: SpaceMachine;
  option: AutocompactProvider;
  writesDisabled: boolean;
  onRecordChange: (machine: SpaceMachine) => void;
}) {
  const saved = record.provider_autocompact[option.provider] ?? "";
  const [draft, setDraft] = useState(saved);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const value = draft.trim();
  const label = `${option.label} auto-compact`;
  async function save() {
    setSaving(true);
    setError(null);
    try {
      const updated = await updateSpaceMachine(record.machine_id, {
        provider_autocompact: { ...record.provider_autocompact, [option.provider]: value },
      });
      onRecordChange(updated);
      setDraft(updated.provider_autocompact[option.provider] ?? "");
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSaving(false);
    }
  }
  return (
    <section className="machine-browser-row" aria-label={label}>
      <header>
        <strong>{label}</strong>
        <input
          value={draft}
          maxLength={16}
          placeholder="CLI default"
          aria-label={label}
          data-autocompact-provider={option.provider}
          disabled={writesDisabled || saving}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && value !== saved) void save();
          }}
        />
      </header>
      <div className="provider-login-notice-actions">
        <button
          type="button"
          className="button secondary compact"
          disabled={writesDisabled || saving || value === saved}
          onClick={() => void save()}
        >
          Save
        </button>
      </div>
      <p>Accepts {option.hint}. Empty keeps the CLI's default.</p>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
