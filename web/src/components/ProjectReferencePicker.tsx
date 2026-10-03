import { useEffect, useState } from "react";
import { api } from "../api";
import { ArrowLeft, Check, ChevronRight, Folder, Search, X } from "lucide-react";
import { referenceKey, type DraftReference } from "../projectReferences";
import { ReferenceIcon } from "./ReferenceChip";
import type { GraphNode, GraphTargetRef, PaperSnapshot, ProjectArtifact } from "../types";

type FolderId = "reports" | "artifacts" | "nodes";
const FOLDER_LABEL: Record<FolderId, string> = {
  reports: "Reports",
  artifacts: "Artifacts",
  nodes: "Nodes",
};
interface Entry {
  folder: FolderId | null;
  reference: DraftReference;
}

/** RCP's records as a small file browser: folders first, then items; search spans all. */
export function ProjectReferencePicker({
  projectId,
  target,
  nodes,
  selectedKeys,
  onPick,
  onClose,
}: {
  projectId: string;
  target: GraphTargetRef;
  nodes: Readonly<Record<string, GraphNode>>;
  selectedKeys: ReadonlySet<string>;
  onPick: (reference: DraftReference) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [folder, setFolder] = useState<FolderId | null>(null);
  const [artifacts, setArtifacts] = useState<ProjectArtifact[]>([]);
  const [paperAvailable, setPaperAvailable] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    const base = `/api/projects/${encodeURIComponent(projectId)}`;
    void Promise.all([
      api<ProjectArtifact[]>(`${base}/artifacts`, { signal: controller.signal }).then(setArtifacts),
      // Only a saved introduction can be referenced; a draft or no paper cannot.
      api<PaperSnapshot>(`${base}/paper`, { signal: controller.signal }).then((paper) =>
        setPaperAvailable(paper.canonical_available && Boolean(paper.canonical_hash)),
      ),
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
  const entries: Entry[] = [
    ...artifacts
      .filter((item) => item.available && item.artifact_id)
      .map((item): Entry => ({
        folder: item.kind === "report" ? "reports" : "artifacts",
        reference: {
          selector: { kind: "artifact", artifact_id: item.artifact_id! },
          label: item.name,
          target,
        },
      })),
    ...Object.values(nodes).map((node): Entry => ({
      folder: "nodes",
      reference: {
        selector: {
          kind: "node",
          node_id: node.id,
          branch_id: target.kind === "branch" ? target.branch_id : null,
        },
        label: node.title,
        target,
      },
    })),
    ...(paperAvailable
      ? [
          {
            folder: null,
            reference: {
              selector: { kind: "paper" as const },
              label: "Paper introduction",
              target,
            },
          },
        ]
      : []),
  ];
  const terms = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const searching = terms.length > 0;
  // Search spans everything at the top level and stays inside an open folder.
  const shown = entries.filter((entry) =>
    searching
      ? (folder === null || entry.folder === folder) &&
        terms.every((term) => entry.reference.label.toLocaleLowerCase().includes(term))
      : entry.folder === folder,
  );
  const folders = (Object.keys(FOLDER_LABEL) as FolderId[]).map((id) => ({
    id,
    count: entries.filter((entry) => entry.folder === id).length,
  }));
  const back = () => {
    setFolder(null);
    setQuery("");
  };
  return (
    <section className="chat-reference-picker" aria-label="From RCP">
      <div className="chat-reference-picker-heading">
        {folder ? (
          <button type="button" aria-label="Back to RCP" onClick={back}>
            <ArrowLeft size={13} />
          </button>
        ) : (
          <Search size={13} aria-hidden="true" />
        )}
        <input
          autoFocus
          aria-label="Search RCP"
          placeholder={folder ? `Search ${FOLDER_LABEL[folder]}` : "Search RCP"}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            if (folder || query) back();
            else onClose();
          }}
        />
        <button type="button" aria-label="Close" onClick={onClose}>
          <X size={13} />
        </button>
      </div>
      {folder && <div className="chat-reference-path">RCP / {FOLDER_LABEL[folder]}</div>}
      {loading && <span role="status">Loading…</span>}
      {error && <div role="alert">{error}</div>}
      <ul>
        {!searching &&
          !folder &&
          folders.map(({ id, count }) => (
            <li key={id}>
              <button type="button" disabled={count === 0} onClick={() => setFolder(id)}>
                <Folder size={13} aria-hidden="true" />
                <span>{FOLDER_LABEL[id]}</span>
                <small>{count}</small>
                <ChevronRight size={13} aria-hidden="true" />
              </button>
            </li>
          ))}
        {shown.map(({ folder: entryFolder, reference }) => {
          const added = selectedKeys.has(referenceKey(reference.selector));
          return (
            <li key={referenceKey(reference.selector)}>
              <button type="button" disabled={added} onClick={() => onPick(reference)}>
                <ReferenceIcon kind={reference.selector.kind} />
                <span>{reference.label}</span>
                {added ? (
                  <Check size={13} aria-label="Added" />
                ) : (
                  searching && !folder && entryFolder && <small>{FOLDER_LABEL[entryFolder]}</small>
                )}
              </button>
            </li>
          );
        })}
        {!loading && !error && searching && shown.length === 0 && (
          <li className="chat-reference-empty">No matches.</li>
        )}
      </ul>
    </section>
  );
}
