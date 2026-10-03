import { Moon } from "lucide-react";
import { useState } from "react";
import {
  browserTimeZone,
  CONSOLIDATION_SETTINGS_ANCHOR,
  DEFAULT_CONSOLIDATION_TIME,
  disableConsolidation,
  enableConsolidation,
} from "../consolidation";
import type { ConsolidationSchedule } from "../types";

interface Props {
  apiBase: string;
  schedule: ConsolidationSchedule | null;
  loaded: boolean;
  loadError: string | null;
  writesDisabled: boolean;
  onChanged: () => void;
}

const OUTCOME_LABELS: Record<NonNullable<ConsolidationSchedule["last_outcome"]>, string> = {
  succeeded: "Succeeded",
  failed: "Failed",
  skipped: "Skipped, no change",
};

function formatInstant(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

/** Enable, renew, or turn off the project's nightly graph consolidation. */
export function ProjectConsolidation({
  apiBase,
  schedule,
  loaded,
  loadError,
  writesDisabled,
  onChanged,
}: Props) {
  const [localTime, setLocalTime] = useState(schedule?.local_time ?? DEFAULT_CONSOLIDATION_TIME);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timezone = schedule?.timezone ?? browserTimeZone();

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };
  const disabled = busy || writesDisabled || !loaded;
  const enable = () => run(() => enableConsolidation(apiBase, localTime, browserTimeZone()));
  const renew = () =>
    run(() => enableConsolidation(apiBase, schedule!.local_time, schedule!.timezone));

  return (
    <section className="settings-section project-consolidation" id={CONSOLIDATION_SETTINGS_ANCHOR}>
      <header>
        <span>
          <Moon size={16} />
        </span>
        <h2>Nightly consolidation</h2>
      </header>
      {schedule ? (
        <dl className="consolidation-schedule">
          <div>
            <dt>Status</dt>
            <dd className={schedule.expired ? "consolidation-expired" : undefined}>
              {schedule.expired ? "Expired" : "On"}
            </dd>
          </div>
          <div>
            <dt>Time</dt>
            <dd>
              {schedule.local_time} {schedule.timezone}
            </dd>
          </div>
          <div>
            <dt>Authorized by</dt>
            <dd>{schedule.authorized_by.display_name || schedule.authorized_by.user_id}</dd>
          </div>
          <div>
            <dt>{schedule.expired ? "Expired" : "Expires"}</dt>
            <dd>{new Date(schedule.expires_at).toLocaleDateString()}</dd>
          </div>
          {schedule.expired ? null : (
            <div>
              <dt>Next run</dt>
              <dd>{formatInstant(schedule.next_due_at)}</dd>
            </div>
          )}
          <div>
            <dt>Last outcome</dt>
            <dd>
              {schedule.last_outcome && schedule.last_run_at
                ? `${OUTCOME_LABELS[schedule.last_outcome]} · ${formatInstant(schedule.last_run_at)}`
                : "None yet"}
            </dd>
          </div>
        </dl>
      ) : (
        <div className="consolidation-enable">
          <label>
            Time
            <input
              type="time"
              value={localTime}
              disabled={disabled}
              onChange={(event) => setLocalTime(event.target.value)}
            />
          </label>
          <span className="mono">{timezone}</span>
        </div>
      )}
      <div className="project-member-actions">
        {schedule ? (
          <>
            <button type="button" disabled={disabled} onClick={() => void renew()}>
              Renew
            </button>
            <button
              type="button"
              disabled={disabled}
              onClick={() => void run(() => disableConsolidation(apiBase))}
            >
              Turn off
            </button>
          </>
        ) : (
          <button
            type="button"
            disabled={disabled || !/^\d{2}:\d{2}$/.test(localTime)}
            onClick={() => void enable()}
          >
            Enable
          </button>
        )}
      </div>
      {loadError ? <p className="project-member-error">{loadError}</p> : null}
      {error ? <p className="project-member-error">{error}</p> : null}
    </section>
  );
}
