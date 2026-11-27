import { useEffect, useState } from "react";
import { loadLimitResume, saveLimitResume } from "../core/api";
import { errorMessage } from "../core/errors";

/** The member's own "Resume after usage limits" switch; it reads the same in every chat. */
export function LimitResumeToggle({ onChanged }: { onChanged: () => Promise<void> }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    void loadLimitResume().then(
      (setting) => {
        if (!cancelled) setEnabled(setting.enabled);
      },
      (failure: unknown) => {
        if (!cancelled) setError(errorMessage(failure));
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);
  async function change(value: boolean) {
    const previous = enabled;
    setEnabled(value);
    setSaving(true);
    setError(null);
    try {
      setEnabled((await saveLimitResume(value)).enabled);
      void onChanged();
    } catch (failure) {
      setEnabled(previous);
      setError(errorMessage(failure));
    } finally {
      setSaving(false);
    }
  }
  return (
    <div className="chat-browser-control" title={error ?? undefined}>
      <div className="browser-consent">
        <label className="browser-consent-switch">
          <input
            type="checkbox"
            role="switch"
            data-setting="limit-resume"
            checked={enabled ?? false}
            disabled={saving || enabled === null}
            onChange={(event) => void change(event.target.checked)}
          />{" "}
          Resume after usage limits
        </label>
      </div>
    </div>
  );
}
