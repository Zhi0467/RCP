import type { EvidenceFlag, GraphNode, Repository } from "../core/types";
import { resolveRepositoryFileHref, type RepositoryFileTarget } from "../core/repositoryFileLinks";
import { isOutcome, lifecycleField, projectType } from "../graph/researchType";

export type EvidenceFlagSource =
  { kind: "file"; target: RepositoryFileTarget } | { kind: "url"; href: string };
export type EvidenceFlagAction =
  "address" | "open_chat" | "open_evidence" | "open_source" | "dismiss";
export interface EvidenceFlagRow {
  flag: EvidenceFlag;
  /** The current Evidence node, or null once it is absent or retired. */
  node: GraphNode | null;
  title: string;
  source: EvidenceFlagSource | null;
  actions: EvidenceFlagAction[];
}

/**
 * Every open record stays listed until dismissed; only current Evidence takes graph marks.
 * `nodes` is the open graph. Off main it cannot decide Address, which main's
 * endpoint checks, so every open flag offers it there.
 */
export function evidenceFlagRows(
  flags: readonly EvidenceFlag[],
  nodes: Record<string, GraphNode>,
  repositories: readonly Repository[],
  onMain = true,
): EvidenceFlagRow[] {
  return flags.flatMap((flag) => {
    if (flag.state === "dismissed") return [];
    const recorded = nodes[flag.evidence_id];
    const node =
      recorded &&
      isOutcome(recorded.type) &&
      recorded[lifecycleField(recorded.type)] !== projectType().retiredValue
        ? recorded
        : null;
    const source = flag.source ? flagSourceTarget(flag.source, repositories) : null;
    const actions: EvidenceFlagAction[] = [];
    if (flag.state === "addressing") actions.push("open_chat");
    else if (node || !onMain) actions.push("address");
    if (node) actions.push("open_evidence");
    if (source) actions.push("open_source");
    actions.push("dismiss");
    return [{ flag, node, title: recorded?.title ?? flag.evidence_id, source, actions }];
  });
}

/** An external URL, or a cited path that resolves to a repository file; otherwise null. */
function flagSourceTarget(
  source: string,
  repositories: readonly Repository[],
): EvidenceFlagSource | null {
  if (/^https?:\/\//.test(source)) return { kind: "url", href: source };
  const repository = repositories.find((repo) => source.startsWith(`${repo.alias}/`));
  const href =
    !source.startsWith("/") && repository
      ? `${repository.path.replace(/\/+$/, "")}/${source.slice(repository.alias.length + 1)}`
      : source;
  const resolution = resolveRepositoryFileHref(href, repositories);
  return resolution.kind === "resolved" ? { kind: "file", target: resolution.target } : null;
}
