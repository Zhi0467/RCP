import { Clock3, ExternalLink, X } from "lucide-react";
import { taskKindLabel, taskStatusLabel } from "../agentTasks";
import type { AgentTask, RevisionSummary } from "../types";

interface Props {
  summaries: RevisionSummary[];
  tasks: AgentTask[];
  loading: boolean;
  error: string | null;
  onInspectTask: (taskId: string) => void;
  campaignReportHref: (campaignId: string, reportId: string) => string;
  onClose: () => void;
}

type HistoryEntry =
  | { kind: "revision"; summary: RevisionSummary }
  | { kind: "campaign"; campaignId: string; summaries: RevisionSummary[] };

export function ProjectHistoryDrawer({
  summaries,
  tasks,
  loading,
  error,
  onInspectTask,
  campaignReportHref,
  onClose,
}: Props) {
  const historyEntries = groupCampaignRevisions(summaries);

  return (
    <div
      className="drawer-scrim"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <aside className="detail-drawer run-inspector" aria-label="Project history">
        <header>
          <h2>Project history</h2>
          <button className="icon-button" aria-label="Close project history" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="run-inspector-body">
          <nav className="run-history" aria-label="Agent tasks">
            <span className="eyebrow">Agent tasks</span>
            {tasks.map((task) => (
              <button
                data-task-id={task.operation_id}
                key={task.operation_id}
                onClick={() => onInspectTask(task.operation_id)}
                type="button"
              >
                <span className={`run-state-dot ${task.status}`} />
                <span className="run-history-copy">
                  <strong>
                    {taskKindLabel(task.kind)} · attempt {task.attempt}
                  </strong>
                  <span className="run-history-meta">
                    {taskStatusLabel(task)} · {formatTimestamp(task.created_at)}
                  </span>
                </span>
              </button>
            ))}
            {tasks.length === 0 && <span className="quiet-empty">No Agent tasks yet.</span>}
          </nav>

          <div className="run-inspector-detail">
            <section aria-label="Project revision summaries">
              <h4>Project revisions</h4>
              {loading ? (
                <div className="quiet-empty" role="status">
                  Loading project revisions…
                </div>
              ) : error ? (
                <div className="quiet-empty" role="alert">
                  {error}
                </div>
              ) : historyEntries.length > 0 ? (
                <ol className="run-events">
                  {historyEntries.map((entry) =>
                    entry.kind === "campaign" ? (
                      <CampaignRevisionGroup
                        campaignId={entry.campaignId}
                        summaries={entry.summaries}
                        campaignReportHref={campaignReportHref}
                        key={`campaign:${entry.campaignId}`}
                      />
                    ) : (
                      <RevisionItem
                        summary={entry.summary}
                        key={`revision:${entry.summary.to_revision}`}
                      />
                    ),
                  )}
                </ol>
              ) : (
                <div className="quiet-empty">No project revisions yet.</div>
              )}
            </section>
          </div>
        </div>
      </aside>
    </div>
  );
}

function CampaignRevisionGroup({
  campaignId,
  summaries,
  campaignReportHref,
}: {
  campaignId: string;
  summaries: RevisionSummary[];
  campaignReportHref: Props["campaignReportHref"];
}) {
  const newest = summaries[0];
  const campaign = newest.campaign;
  const report = campaign?.report;
  const labelId = `history-campaign-${newest.to_revision}`;
  const authorizer = newest.authorized_by?.display_name ?? "Unattributed";

  return (
    <li className="history-campaign-group">
      <Clock3 size={12} />
      <section aria-labelledby={labelId}>
        <header className="history-campaign-header">
          <div>
            <h5 id={labelId}>
              {campaign
                ? `Campaign · ${campaignStateLabel(campaign.state)}`
                : "Campaign no longer recorded"}
            </h5>
            <p className="history-campaign-meta">
              Authorized by {authorizer} ·{" "}
              <time dateTime={newest.created_at}>{formatTimestamp(newest.created_at)}</time> ·{" "}
              {summaries.length} {summaries.length === 1 ? "revision" : "revisions"}
            </p>
          </div>
          {report && (
            <a
              className="button compact secondary"
              href={campaignReportHref(campaignId, report.report_id)}
              aria-label={`Open the ${report.ending} campaign report`}
              target="_blank"
              rel="noopener noreferrer"
            >
              <ExternalLink size={12} /> Open report
            </a>
          )}
        </header>
        <ol className="history-campaign-revisions">
          {summaries.map((summary) => (
            <RevisionItem summary={summary} key={summary.to_revision} />
          ))}
        </ol>
      </section>
    </li>
  );
}

function RevisionItem({ summary }: { summary: RevisionSummary }) {
  return (
    <li className="info">
      <Clock3 size={12} />
      <div>
        <p>
          <strong>
            Revision {summary.from_revision} to revision {summary.to_revision}
          </strong>
        </p>
        {summary.sentences.map((sentence, index) => (
          <p key={`${summary.to_revision}:${index}`}>{sentence}</p>
        ))}
        <time dateTime={summary.created_at}>
          {revisionKindLabel(summary.kind)} · {revisionAttribution(summary)} ·{" "}
          {formatTimestamp(summary.created_at)}
        </time>
        {summary.producer === "agent" && summary.authorized_by && (
          <p className="history-attribution-detail">
            {summary.profile === "orchestrator" ? "Orchestrator" : "Ordinary"} Agent task
            {summary.task_id ? ` · ${summary.task_id}` : ""}
          </p>
        )}
      </div>
    </li>
  );
}

function groupCampaignRevisions(summaries: RevisionSummary[]): HistoryEntry[] {
  const newestFirst = [...summaries].sort((left, right) => right.to_revision - left.to_revision);
  const byCampaign = new Map<string, RevisionSummary[]>();
  for (const summary of newestFirst) {
    if (!summary.campaign_id) continue;
    const group = byCampaign.get(summary.campaign_id) ?? [];
    group.push(summary);
    byCampaign.set(summary.campaign_id, group);
  }

  const seenCampaigns = new Set<string>();
  const entries: HistoryEntry[] = [];
  for (const summary of newestFirst) {
    if (!summary.campaign_id) {
      entries.push({ kind: "revision", summary });
      continue;
    }
    if (seenCampaigns.has(summary.campaign_id)) continue;
    seenCampaigns.add(summary.campaign_id);
    entries.push({
      kind: "campaign",
      campaignId: summary.campaign_id,
      summaries: byCampaign.get(summary.campaign_id) ?? [summary],
    });
  }
  return entries;
}

function campaignStateLabel(state: NonNullable<RevisionSummary["campaign"]>["state"]): string {
  return state === "completed" ? "Completed" : capitalize(state);
}

function revisionAttribution(summary: RevisionSummary): string {
  if (summary.producer === "system") return "RCP";
  if (summary.authorized_by) return summary.authorized_by.display_name;
  const role = summary.author ? capitalize(summary.author) : capitalize(summary.producer);
  return `${role} · Unattributed`;
}

function revisionKindLabel(kind: RevisionSummary["kind"]): string {
  if (kind === "experiment_loop") return "Experiment loop";
  return capitalize(kind);
}

function capitalize(value: string): string {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}

function formatTimestamp(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
}
