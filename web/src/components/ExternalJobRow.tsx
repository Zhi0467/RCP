import { useEffect, useState } from "react";
import { cancelWatcher } from "../api";
import type { ExternalWatcherRecord } from "../types";

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
  const label = watcher.log_path.split("/").at(-1) || watcher.watcher_id;
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
      <strong>{label}</strong>
      <span>Watcher {cancellation.status}</span>
      <code title="Log path">{watcher.log_path}</code>
      <time dateTime={cancellation.last_checked_at ?? undefined}>
        {cancellation.last_checked_at
          ? `Checked ${new Date(cancellation.last_checked_at).toLocaleString()}`
          : "Not checked yet"}
      </time>
      {cancellation.last_error && <span role="alert">{cancellation.last_error}</span>}
      {cancellation.cancel_requested_by && (
        <span>
          Cancel requested by {cancellation.cancel_requested_by}
          {cancellation.cancel_requested_at &&
            ` · ${new Date(cancellation.cancel_requested_at).toLocaleString()}`}
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
