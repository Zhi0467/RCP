import { useEffect, useState } from "react";
import { cancelWatcher } from "../core/api";
import type { ExternalWatcherRecord } from "../core/types";

export function ExternalJobRow({
  apiBase,
  watcher,
  onHide,
}: {
  apiBase: string;
  watcher: ExternalWatcherRecord;
  onHide?: () => void;
}) {
  const [result, setResult] = useState<ExternalWatcherRecord | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Existing watcher refreshes own observation; no additional job-list request is needed.
  // The backend's can_cancel owns availability; view locks never disable Cancel.
  useEffect(() => setResult(null), [watcher]);
  const cancellation = result ?? watcher;
  const label = jobLabel(watcher.log_path) || watcher.watcher_id;
  const cancel = async () => {
    if (cancelling) return;
    setCancelling(true);
    setError(null);
    try {
      setResult(await cancelWatcher(apiBase, watcher.watcher_id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setCancelling(false);
    }
  };
  return (
    <>
      <strong title={watcher.log_path}>{label}</strong>
      <span className={`watcher-state ${cancellation.status}`}>
        {WATCHER_STATE_LABELS[cancellation.status] ?? cancellation.status}
      </span>
      <span className="watcher-meta">
        <time dateTime={cancellation.last_checked_at ?? undefined}>
          {cancellation.last_checked_at
            ? `Checked ${shortTime(cancellation.last_checked_at)}`
            : "Not checked yet"}
        </time>
        <code title={watcher.log_path}>{shortPath(watcher.log_path)}</code>
      </span>
      {cancellation.last_error && <span role="alert">{cancellation.last_error}</span>}
      {cancellation.cancel_requested_by && (
        <span className="watcher-note">
          Cancel requested by {cancellation.cancel_requested_by_name || "a member"}
          {cancellation.cancel_requested_at && ` · ${shortTime(cancellation.cancel_requested_at)}`}
        </span>
      )}
      {(error || cancellation.cancel_error) && (
        <span role="alert">{error || cancellation.cancel_error}</span>
      )}
      {cancellation.can_cancel && (
        <button
          type="button"
          className="button compact watcher-action"
          onClick={() => void cancel()}
          disabled={cancelling}
          aria-label={`Cancel job ${label}`}
        >
          {cancelling ? "Cancelling…" : "Cancel"}
        </button>
      )}
      {!cancellation.can_cancel && watcher.status === "completed" && onHide && (
        <button
          type="button"
          className="button compact watcher-action"
          onClick={onHide}
          aria-label={`Hide watcher ${label}`}
        >
          Hide
        </button>
      )}
    </>
  );
}

const WATCHER_STATE_LABELS: Record<string, string> = {
  active: "Watching",
  degraded: "Check failing",
  completed: "Finished",
  stopped: "Stopped",
};

const GENERIC_LOG_NAMES = new Set(["log", "stdout", "stderr", "output", "out"]);

/** A bare "log" says nothing; keep its parent folder so rows stay distinguishable. */
function jobLabel(path: string): string {
  const parts = path.split("/").filter(Boolean);
  const name = parts.at(-1) ?? "";
  if (!GENERIC_LOG_NAMES.has(name) || parts.length < 2) return name;
  const folder = parts.at(-2) ?? "";
  return `${folder.length > 16 ? `${folder.slice(0, 8)}…` : folder}/${name}`;
}

function shortPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.length > 3 ? `…/${parts.slice(-3).join("/")}` : path;
}

function shortTime(value: string): string {
  const date = new Date(value);
  const sameDay = date.toDateString() === new Date().toDateString();
  return date.toLocaleString(
    undefined,
    sameDay
      ? { hour: "numeric", minute: "2-digit" }
      : { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" },
  );
}
