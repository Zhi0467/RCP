import { TriangleAlert, ArrowRight, Bookmark, ExternalLink, X } from "lucide-react";
import { useState } from "react";
import { consolidationAttentionCount } from "./consolidation";
import type { ConsolidationInboxItem, ConsolidationSchedule } from "../core/types";

interface Props {
  items: ConsolidationInboxItem[];
  schedule: ConsolidationSchedule | null;
  needsRenewal: boolean;
  error: string | null;
  writesDisabled: boolean;
  onOpenReport: (artifactId: string) => void;
  onResolve: (runId: string, action: "keep" | "dismiss") => Promise<void>;
  onOpenSettings: () => void;
}

/** Open consolidation reports and failures, plus one renewal row near expiry. */
export function ConsolidationInbox({
  items: loadedItems,
  schedule,
  needsRenewal,
  error,
  writesDisabled,
  onOpenReport,
  onResolve,
  onOpenSettings,
}: Props) {
  const [busyRun, setBusyRun] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // A closed row stays hidden until the refresh that drops it lands.
  const [closedRuns, setClosedRuns] = useState<ReadonlySet<string>>(new Set());
  const items = loadedItems.filter((item) => !closedRuns.has(item.run_id));
  if (items.length === 0 && !needsRenewal && !error) return null;

  const resolve = async (runId: string, action: "keep" | "dismiss") => {
    setBusyRun(runId);
    setActionError(null);
    try {
      await onResolve(runId, action);
      setClosedRuns((closed) => new Set(closed).add(runId));
    } catch (failure) {
      setActionError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusyRun(null);
    }
  };

  return (
    <section className="consolidation-inbox" aria-label="Consolidation">
      <header className="rail-heading proposal-section-heading">
        <h2>Consolidation</h2>
        <span className="count-badge">{consolidationAttentionCount(items, needsRenewal)}</span>
      </header>
      {error ? <p className="consolidation-error">{error}</p> : null}
      {actionError ? <p className="consolidation-error">{actionError}</p> : null}
      {needsRenewal && schedule ? (
        <button className="attention-item blocker" type="button" onClick={onOpenSettings}>
          <TriangleAlert size={16} />
          <strong>
            {schedule.expired
              ? "Nightly consolidation expired"
              : `Nightly consolidation expires ${new Date(schedule.expires_at).toLocaleDateString()}`}
          </strong>
          <ArrowRight size={14} />
        </button>
      ) : null}
      {items.map((item) => {
        const busy = busyRun === item.run_id || writesDisabled;
        return (
          <article className={`consolidation-row ${item.kind}`} key={item.run_id}>
            <div className="proposal-topline">
              <span className="eyebrow">{item.kind === "report" ? "Report" : "Failed"}</span>
              <span className="mono">{item.occurrence_date}</span>
            </div>
            {item.kind === "report" && item.report ? (
              <h3>{item.report.title}</h3>
            ) : (
              <h3>{item.error?.message ?? "Consolidation failed"}</h3>
            )}
            <dl className="consolidation-counts">
              <div>
                <dt>Revisions</dt>
                <dd>{item.revisions_verified ? item.applied_revisions.length : "Unverified"}</dd>
              </div>
              <div>
                <dt>Proposals</dt>
                <dd>{item.proposals_created}</dd>
              </div>
            </dl>
            {item.kind === "failure" && item.applied_revisions.length > 0 ? (
              <ul className="consolidation-revisions">
                {item.applied_revisions.map((revision) => (
                  <li key={revision.revision}>
                    <span className="mono">rev {revision.revision}</span> {revision.summary}
                  </li>
                ))}
              </ul>
            ) : null}
            <div className="card-actions">
              {item.kind === "report" && item.report ? (
                <>
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => onOpenReport(item.report!.artifact_id)}
                  >
                    <ExternalLink size={14} /> Open
                  </button>
                  <button
                    className="button secondary"
                    type="button"
                    disabled={busy}
                    onClick={() => void resolve(item.run_id, "keep")}
                  >
                    <Bookmark size={14} /> Keep
                  </button>
                </>
              ) : null}
              <button
                className="button secondary"
                type="button"
                disabled={busy}
                onClick={() => void resolve(item.run_id, "dismiss")}
              >
                <X size={14} /> Dismiss
              </button>
            </div>
          </article>
        );
      })}
    </section>
  );
}
