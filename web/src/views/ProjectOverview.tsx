import {
  Check,
  ExternalLink,
  FileText,
  GitBranch,
  GitCommitHorizontal,
  Sparkles,
  TriangleAlert,
} from "lucide-react";
import type { ReactNode } from "react";
import { currentExperimentGuidance } from "../experimentGuidance";
import {
  editCountLabel,
  needsYouKindLabel,
  projectDigestIsEmpty,
  ranKindLabel,
} from "../projectDigest";
import type {
  AppView,
  GraphNode,
  GraphState,
  ProjectDigest,
  ProjectSnapshot,
  Proposal,
  RevisionSummary,
} from "../types";

export interface DigestCardProps {
  digest: ProjectDigest | null;
  error: string | null;
  catchingUp: boolean;
  onCatchUp: () => void;
  onOpenArtifact: (artifactId: string) => void;
}

interface Props {
  project: ProjectSnapshot;
  graph: GraphState;
  pendingProposals: Proposal[];
  decisionsAwaitingChoice: GraphNode[];
  latestRevisionSummary?: RevisionSummary | null;
  onNavigate: (view: AppView) => void;
  digest: DigestCardProps;
}

export function ProjectOverview({
  project,
  graph,
  pendingProposals,
  decisionsAwaitingChoice,
  latestRevisionSummary,
  onNavigate,
  digest,
}: Props) {
  const nodes = Object.values(graph.nodes);
  const activeExperiments = nodes.filter(
    (node) => node.type === "experiment" && !project.experiment_control[node.id]?.node_closed,
  );
  const latestNode = [...nodes].sort((left, right) => right.updated_rev - left.updated_rev)[0];
  const blockers = nodes.filter((node) => node.type === "blocker" && node.status === "open");
  const nextExperiment = activeExperiments.find((node) =>
    currentExperimentGuidance(node, "next_action"),
  );
  const nextExperimentAction = nextExperiment
    ? currentExperimentGuidance(nextExperiment, "next_action")
    : null;
  const question =
    project.primary_question?.question ||
    project.primary_question?.title ||
    "No primary research question has been seeded.";
  const latestRevisionText = latestRevisionSummary?.sentences.slice(0, 2).join(" ").trim();
  const latestRevisionDetail =
    latestRevisionSummary && latestRevisionText
      ? `Revision ${latestRevisionSummary.from_revision} to revision ${latestRevisionSummary.to_revision}`
      : null;

  const rows: Array<{
    number: string;
    prompt: string;
    answer: string;
    detail: string;
    view: AppView;
    node?: GraphNode;
  }> = [
    {
      number: "01",
      prompt: "What are we asking?",
      answer: String(question),
      detail: `${nodes.filter((node) => node.type === "hypothesis").length} hypotheses · ${project.counts.accepted} accepted nodes`,
      view: "scientific",
    },
    {
      number: "02",
      prompt: "Where are we?",
      answer: activeExperiments.length
        ? `${activeExperiments.length} active experiment${activeExperiments.length === 1 ? "" : "s"}`
        : !project.last_refresh_at
          ? "The project has not been seeded yet."
          : "Understanding and review",
      detail: `Project revision ${graph.revision}`,
      view: "execution",
    },
    {
      number: "03",
      prompt: "What changed?",
      answer: latestRevisionText || (latestNode ? latestNode.title : "No graph changes yet."),
      detail: latestRevisionDetail
        ? latestRevisionDetail
        : project.last_refresh_at
          ? `Last refresh ${new Date(project.last_refresh_at).toLocaleString()}`
          : "Never refreshed",
      view: "scientific",
    },
    {
      number: "04",
      prompt: "What is blocked?",
      answer: blockers[0]?.title || "No open blocker is recorded.",
      detail: blockers.length
        ? `${blockers.length} open blocker${blockers.length === 1 ? "" : "s"}`
        : "No open blocker in the graph",
      view: "dag",
    },
    {
      number: "05",
      prompt: "What needs you?",
      answer:
        pendingProposals[0]?.title ||
        decisionsAwaitingChoice[0]?.title ||
        "Nothing currently requires human judgment.",
      detail: `${pendingProposals.length} proposals · ${decisionsAwaitingChoice.length} decisions awaiting choice`,
      view: "attention",
    },
    {
      number: "06",
      prompt: "What happens next?",
      answer: String(
        nextExperimentAction ||
          (!project.last_refresh_at
            ? "Seed the graph from the selected truth repositories."
            : "Refresh when new research work lands."),
      ),
      detail: nextExperiment ? nextExperiment.title : "Project-level next action",
      view: nextExperiment ? "execution" : "attention",
    },
  ];

  return (
    <section className="overview-page">
      <DigestCard {...digest} />
      <header className="overview-heading">
        <div className="overview-revision">
          <span>Project revision · {project.canonical_state.remote ? "remote" : "local"}</span>
          <strong>{String(graph.revision).padStart(3, "0")}</strong>
        </div>
      </header>
      <div className="overview-questions">
        {rows.map((row) => (
          <button key={row.number} onClick={() => onNavigate(row.view)}>
            <span className="overview-number">{row.number}</span>
            <span className="overview-question">
              <small>{row.prompt}</small>
              <strong>{row.answer}</strong>
            </span>
            <span className="overview-detail">{row.detail}</span>
            <ExternalLink size={20} />
          </button>
        ))}
      </div>
    </section>
  );
}

/** "Since you last looked": hidden when empty; Caught up acknowledges what is on screen. */
function DigestCard({ digest, error, catchingUp, onCatchUp, onOpenArtifact }: DigestCardProps) {
  if (!digest || projectDigestIsEmpty(digest)) {
    // A failed read must not look like being caught up.
    if (!error) return null;
    return (
      <section className="digest-card" aria-label="Since you last looked">
        <header className="digest-card-header">
          <div className="digest-heading">
            <span className="digest-eyebrow">
              <Sparkles size={14} />
              Since you last looked
            </span>
          </div>
        </header>
        <p role="alert">{error}</p>
      </section>
    );
  }
  const changes = digest.changed.length + digest.branches.length;
  return (
    <section className="digest-card" aria-label="Since you last looked">
      <header className="digest-card-header">
        <div className="digest-heading">
          <span className="digest-eyebrow">
            <Sparkles size={14} />
            Since you last looked
          </span>
          <h2>{digestSummary(digest)}</h2>
          {digest.mark && <small>Last caught up {formatDigestTime(digest.mark.marked_at)}</small>}
        </div>
        <button
          className="button primary compact"
          type="button"
          disabled={catchingUp}
          onClick={onCatchUp}
        >
          <Check size={14} />
          Caught up
        </button>
      </header>
      {error && <p role="alert">{error}</p>}
      <div className="digest-groups">
        {digest.needs_you.length > 0 && (
          <DigestGroup
            tone="attention"
            icon={<TriangleAlert size={16} />}
            title="Needs you"
            count={digest.needs_you.length}
          >
            {digest.needs_you.map((item) => (
              <li className="digest-row" key={`${item.kind}:${item.item_id}`}>
                <span className="digest-kind">{needsYouKindLabel(item)}</span>
                <DigestLink href={item.deep_link}>{item.title}</DigestLink>
              </li>
            ))}
          </DigestGroup>
        )}
        {changes > 0 && (
          <DigestGroup
            tone="change"
            icon={<GitCommitHorizontal size={16} />}
            title="Changed on main"
            count={changes}
          >
            {digest.changed.map((change) => (
              <li className="digest-row" key={change.source_key}>
                <DigestLink href={change.deep_link}>{change.label}</DigestLink>
                <span className="digest-row-meta">
                  <span className="digest-pill">{editCountLabel(change.edits)}</span>
                  {change.report_artifact_id && (
                    <button
                      className="digest-report"
                      type="button"
                      onClick={() => onOpenArtifact(change.report_artifact_id!)}
                    >
                      <FileText size={14} />
                      Report
                    </button>
                  )}
                </span>
              </li>
            ))}
            {digest.branches.map((branch) => (
              <li className="digest-row" key={`branch:${branch.episode_id}`}>
                <DigestLink href={branch.deep_link}>{branch.title}</DigestLink>
                <span className="digest-row-meta">
                  <span className="digest-pill branch">
                    <GitBranch size={12} />
                    {editCountLabel(branch.edits)} on its branch
                  </span>
                </span>
              </li>
            ))}
          </DigestGroup>
        )}
        {digest.ran.length > 0 && (
          <DigestGroup tone="ran" icon={<Check size={16} />} title="Ran" count={digest.ran.length}>
            {digest.ran.map((item) => (
              <li className="digest-row" key={`${item.kind}:${item.item_id}`}>
                <span className="digest-kind">{ranKindLabel(item)}</span>
                <DigestLink href={item.deep_link}>{item.title}</DigestLink>
                {item.status && (
                  <span className="digest-row-meta">
                    <span className={`digest-pill status-${digestStatusTone(item.status)}`}>
                      {item.status}
                    </span>
                  </span>
                )}
              </li>
            ))}
          </DigestGroup>
        )}
      </div>
    </section>
  );
}

function DigestGroup({
  tone,
  icon,
  title,
  count,
  children,
}: {
  tone: "attention" | "change" | "ran";
  icon: ReactNode;
  title: string;
  count: number;
  children: ReactNode;
}) {
  return (
    <div className={`digest-group ${tone}`}>
      <h3>
        {icon}
        {title}
        <span className="digest-group-count">{count}</span>
      </h3>
      <ul>{children}</ul>
    </div>
  );
}

function digestSummary(digest: ProjectDigest): string {
  const edits =
    digest.changed.reduce((total, change) => total + change.edits, 0) +
    digest.branches.reduce((total, branch) => total + branch.edits, 0);
  const parts = [
    digest.needs_you.length > 0 &&
      `${digest.needs_you.length} need${digest.needs_you.length === 1 ? "s" : ""} you`,
    edits > 0 && editCountLabel(edits),
    digest.ran.length > 0 && `${digest.ran.length} finished`,
  ].filter(Boolean);
  return parts.join(" · ");
}

function digestStatusTone(status: string): "bad" | "good" | "neutral" {
  if (/fail|error|lost|cancel/i.test(status)) return "bad";
  if (/succe|complete|kept/i.test(status)) return "good";
  return "neutral";
}

function formatDigestTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
}

/** A digest link is the notification link format, resolved by App on hashchange. */
function DigestLink({ href, children }: { href: string | null; children: ReactNode }) {
  return href ? (
    <a className="digest-title" href={href}>
      {children}
    </a>
  ) : (
    <span className="digest-title">{children}</span>
  );
}
