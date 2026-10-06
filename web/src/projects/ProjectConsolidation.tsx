import { Moon } from "lucide-react";
import { useEffect, useState } from "react";
import {
  browserTimeZone,
  CONSOLIDATION_AUTHORIZATION_DAYS,
  CONSOLIDATION_RECENT_NIGHTS,
  CONSOLIDATION_SETTINGS_ANCHOR,
  consolidationAuthorization,
  consolidationCountdown,
  consolidationNeedsRenewal,
  consolidationNightSlots,
  DEFAULT_CONSOLIDATION_TIME,
  disableConsolidation,
  enableConsolidation,
  timeZoneOptions,
} from "./consolidation";
import type { ConsolidationNight, ConsolidationSchedule } from "../core/types";

interface Props {
  apiBase: string;
  schedule: ConsolidationSchedule | null;
  /** The newest nights, oldest first, as GET /consolidation serves them. */
  recentNights: ConsolidationNight[];
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

const NIGHT_LABELS: Record<ConsolidationNight["outcome"], string> = {
  ...OUTCOME_LABELS,
  running: "Running",
};

function formatInstant(value: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

/** An occurrence date is a calendar day, so read it at UTC midnight and print it in UTC. */
function formatNight(occurrenceDate: string, options: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" }).format(
    new Date(`${occurrenceDate}T00:00:00Z`),
  );
}

/** The current time, refreshed once a minute while `active`. */
function useMinuteClock(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function NightStrip({ nights }: { nights: ConsolidationNight[] }) {
  return (
    <div className="consolidation-nights">
      <span className="consolidation-label" id="consolidation-nights-label">
        Last {CONSOLIDATION_RECENT_NIGHTS} nights
      </span>
      <ol aria-labelledby="consolidation-nights-label">
        {consolidationNightSlots(nights).map((night, index) => {
          if (!night) {
            return (
              <li key={`empty-${index}`} className="consolidation-night empty" aria-hidden="true">
                <span className="consolidation-night-dot" />
                <span>&nbsp;</span>
              </li>
            );
          }
          const label = `${formatNight(night.occurrence_date, { dateStyle: "medium" })}: ${NIGHT_LABELS[night.outcome]}`;
          return (
            <li
              key={night.occurrence_date}
              className={`consolidation-night ${night.outcome}`}
              title={label}
            >
              <span className="consolidation-night-dot" role="img" aria-label={label} />
              <span aria-hidden="true">
                {formatNight(night.occurrence_date, { weekday: "narrow" })}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function AuthorizationBar({ schedule, now }: { schedule: ConsolidationSchedule; now: number }) {
  const { daysLeft, fraction, expired } = consolidationAuthorization(schedule, now);
  const expiresOn = new Date(schedule.expires_at).toLocaleDateString();
  const state = expired ? "expired" : consolidationNeedsRenewal(schedule, now) ? "renew" : "ok";
  return (
    <div className={`consolidation-authorization ${state}`}>
      <div className="consolidation-authorization-row">
        <span className="consolidation-label" id="consolidation-authorization-label">
          Authorization
        </span>
        <span className={expired ? "consolidation-expired" : undefined}>
          {expired
            ? `Expired ${expiresOn}`
            : `${daysLeft} ${daysLeft === 1 ? "day" : "days"} left · ${expiresOn}`}
        </span>
      </div>
      <div
        className="consolidation-authorization-track"
        role="meter"
        aria-labelledby="consolidation-authorization-label"
        aria-valuemin={0}
        aria-valuemax={CONSOLIDATION_AUTHORIZATION_DAYS}
        aria-valuenow={daysLeft}
        aria-valuetext={
          expired ? "Expired" : `${daysLeft} of ${CONSOLIDATION_AUTHORIZATION_DAYS} days left`
        }
      >
        <span style={{ width: `${fraction * 100}%` }} />
      </div>
    </div>
  );
}

/** The countdown, then the run's wall-clock time and zone where the viewer is. */
function nextRunText(nextDueAt: string, now: number): string {
  const countdown = consolidationCountdown(nextDueAt, now);
  const at = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(nextDueAt));
  if (!countdown) return `Due now · ${at}`;
  const left =
    countdown.hours > 0 ? `${countdown.hours}h ${countdown.minutes}m` : `${countdown.minutes}m`;
  return `In ${left} · ${at}`;
}

/** Enable, renew, or turn off the project's nightly graph consolidation. */
export function ProjectConsolidation({
  apiBase,
  schedule,
  recentNights,
  loaded,
  loadError,
  writesDisabled,
  onChanged,
}: Props) {
  // Unsaved edits; null shows the saved schedule, or the defaults before one exists.
  const [draftTime, setDraftTime] = useState<string | null>(null);
  const [draftZone, setDraftZone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const localTime = draftTime ?? schedule?.local_time ?? DEFAULT_CONSOLIDATION_TIME;
  const timezone = draftZone ?? schedule?.timezone ?? browserTimeZone();
  const edited =
    schedule !== null && (localTime !== schedule.local_time || timezone !== schedule.timezone);
  const now = useMinuteClock(schedule !== null);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      setDraftTime(null);
      setDraftZone(null);
      onChanged();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };
  const disabled = busy || writesDisabled || !loaded;
  // Enable, Save, and Renew all PUT the schedule, which also restarts the authorization.
  const save = () => run(() => enableConsolidation(apiBase, localTime, timezone));

  return (
    <section className="settings-section project-consolidation" id={CONSOLIDATION_SETTINGS_ANCHOR}>
      <header>
        <span aria-hidden="true">
          <Moon size={16} />
        </span>
        <h2>Nightly consolidation</h2>
      </header>
      {schedule ? (
        <div className="consolidation-sky">
          <dl className="consolidation-schedule">
            <div>
              <dt>Status</dt>
              <dd className={schedule.expired ? "consolidation-expired" : undefined}>
                {schedule.expired ? "Expired" : "On"}
              </dd>
            </div>
            {schedule.expired ? null : (
              <div>
                <dt>Next run</dt>
                <dd title={formatInstant(schedule.next_due_at)}>
                  {nextRunText(schedule.next_due_at, now)}
                </dd>
              </div>
            )}
            <div>
              <dt>Authorized by</dt>
              <dd>{schedule.authorized_by.display_name || schedule.authorized_by.user_id}</dd>
            </div>
            <div>
              <dt>Last outcome</dt>
              <dd>
                {schedule.last_outcome && schedule.last_run_at
                  ? `${OUTCOME_LABELS[schedule.last_outcome]} · ${formatInstant(schedule.last_run_at)}`
                  : "None yet"}
              </dd>
            </div>
          </dl>
          <AuthorizationBar schedule={schedule} now={now} />
          <NightStrip nights={recentNights} />
        </div>
      ) : null}
      <div className="project-member-actions">
        <label>
          Time
          <input
            type="time"
            value={localTime}
            disabled={disabled}
            onChange={(event) => setDraftTime(event.target.value)}
          />
        </label>
        <label>
          Time zone
          <select
            value={timezone}
            disabled={disabled}
            onChange={(event) => setDraftZone(event.target.value)}
          >
            {timeZoneOptions(timezone).map((zone) => (
              <option key={zone} value={zone}>
                {zone}
              </option>
            ))}
          </select>
        </label>
        {schedule ? (
          <>
            <button
              className="button secondary compact"
              type="button"
              disabled={disabled || !/^\d{2}:\d{2}$/.test(localTime)}
              onClick={() => void save()}
            >
              {edited ? "Save" : "Renew"}
            </button>
            <button
              className="button secondary compact"
              type="button"
              disabled={disabled}
              onClick={() => void run(() => disableConsolidation(apiBase))}
            >
              Turn off
            </button>
          </>
        ) : (
          <button
            className="button secondary compact"
            type="button"
            disabled={disabled || !/^\d{2}:\d{2}$/.test(localTime)}
            onClick={() => void save()}
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
