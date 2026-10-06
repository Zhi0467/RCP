import { useState } from "react";
import { updateSpaceMachine } from "../core/api";
import { errorMessage } from "../core/errors";
import type { SpaceMachine } from "../core/types";

/** One provider setting for every turn on this machine, read at each launch. */
export function MachineProviderSettingRow({
  record,
  provider,
  setting,
  label,
  placeholder,
  hint,
  writesDisabled,
  onRecordChange,
}: {
  record: SpaceMachine;
  provider: string;
  setting: "provider_autocompact" | "provider_shell_timeout";
  label: string;
  placeholder: string;
  hint: string;
  writesDisabled: boolean;
  onRecordChange: (machine: SpaceMachine) => void;
}) {
  const saved = record[setting][provider] ?? "";
  const [draft, setDraft] = useState(saved);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const value = draft.trim();
  async function save() {
    setSaving(true);
    setError(null);
    try {
      const updated = await updateSpaceMachine(record.machine_id, {
        // The server merges per provider, so concurrent row saves never erase each other.
        [setting]: { [provider]: value },
      });
      onRecordChange(updated);
      setDraft(updated[setting][provider] ?? "");
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
          placeholder={placeholder}
          title={hint}
          aria-label={label}
          data-provider-setting={setting}
          data-provider={provider}
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
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
