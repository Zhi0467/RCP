import { useEffect, useState } from "react";
import { api } from "../api";
import type { DraftReference } from "../projectReferences";
import type { GraphNode, GraphTargetRef, ProjectArtifact } from "../types";

export function ProjectReferencePicker({
  projectId,
  target,
  nodes,
  onPick,
  onClose,
}: {
  projectId: string;
  target: GraphTargetRef;
  nodes: Readonly<Record<string, GraphNode>>;
  onPick: (reference: DraftReference) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [artifacts, setArtifacts] = useState<ProjectArtifact[]>([]);
  const [paperAvailable, setPaperAvailable] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    const base = `/api/projects/${encodeURIComponent(projectId)}`;
    void Promise.all([
      api<ProjectArtifact[]>(`${base}/artifacts`, { signal: controller.signal }).then(setArtifacts),
      api(`${base}/paper`, { signal: controller.signal }).then(() => setPaperAvailable(true)),
    ])
      .catch((failure) => {
        if (!controller.signal.aborted)
          setError(failure instanceof Error ? failure.message : String(failure));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [projectId]);
  const items: DraftReference[] = [
    ...artifacts
      .filter((item) => item.available && item.artifact_id)
      .map((item): DraftReference => ({
        selector: { kind: "artifact", artifact_id: item.artifact_id! },
        label: item.name,
        target,
      })),
    ...Object.values(nodes).map((node): DraftReference => ({
      selector: {
        kind: "node",
        node_id: node.id,
        branch_id: target.kind === "branch" ? target.branch_id : null,
      },
      label: node.title,
      target,
    })),
    ...(paperAvailable
      ? [{ selector: { kind: "paper" as const }, label: "Paper introduction", target }]
      : []),
  ];
  const terms = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const matches = items.filter((item) =>
    terms.every((term) =>
      `${item.label} ${item.selector.kind} ${JSON.stringify(item.selector)}`
        .toLocaleLowerCase()
        .includes(term),
    ),
  );
  return (
    <section className="chat-reference-picker" aria-label="From project">
      <div className="chat-reference-picker-heading">
        <input
          autoFocus
          aria-label="Search project references"
          placeholder="Search project"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
      {loading && <span role="status">Loading…</span>}
      {error && <div role="alert">{error}</div>}
      <ul>
        {matches.map((item) => (
          <li key={JSON.stringify(item.selector)}>
            <button type="button" onClick={() => onPick(item)}>
              <span>{item.label}</span>
              <small>{item.selector.kind}</small>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
