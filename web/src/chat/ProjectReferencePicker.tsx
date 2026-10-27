import { useEffect, useState } from "react";
import { api } from "../core/api";
import { ArrowLeft, Check, ChevronRight, Folder, RotateCw, Search, X } from "lucide-react";
import { referenceKey, type DraftReference } from "../core/projectReferences";
import { ReferenceIcon } from "../core/ReferenceChip";
import { BranchGroupList, branchGroups } from "../artifacts/BranchGroups";
import type { GraphNode, GraphTargetRef, PaperTree, ProjectArtifact } from "../core/types";

type FolderId = "reports" | "artifacts" | "nodes" | "paper";
const FOLDER_LABEL: Record<FolderId, string> = {
  reports: "Reports",
  artifacts: "Artifacts",
  nodes: "Nodes",
  paper: "Paper",
};
interface Entry {
  folder: FolderId | null;
  reference: DraftReference;
  /** Set for a report or artifact, which an open folder groups by branch. */
  artifact?: ProjectArtifact;
}

/** RCP's records as a small file browser: folders first, then items; search spans all. */
export function ProjectReferencePicker({
  projectId,
  target,
  nodes,
  selectedKeys,
  artifactsOnly = false,
  onPick,
  onClose,
}: {
  projectId: string;
  target: GraphTargetRef;
  nodes: Readonly<Record<string, GraphNode>>;
  artifactsOnly?: boolean;
  selectedKeys: ReadonlySet<string>;
  onPick: (reference: DraftReference) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [folder, setFolder] = useState<FolderId | null>(null);
  const [artifacts, setArtifacts] = useState<ProjectArtifact[]>([]);
  const [paper, setPaper] = useState<PaperTree | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const base = `/api/projects/${encodeURIComponent(projectId)}`;
    void Promise.all([
      api<ProjectArtifact[]>(`${base}/artifacts`, { signal: controller.signal }).then(setArtifacts),
      ...(!artifactsOnly
        ? [api<PaperTree>(`${base}/paper/tree`, { signal: controller.signal }).then(setPaper)]
        : []),
    ])
      .catch((failure) => {
        if (!controller.signal.aborted)
          setError(failure instanceof Error ? failure.message : String(failure));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, artifactsOnly, retry]);
  const entries: Entry[] = [
    ...artifacts
      .filter((item) => item.available && item.artifact_id)
      .map((item): Entry => ({
        folder: item.kind === "report" ? "reports" : "artifacts",
        artifact: item,
        reference: {
          selector: { kind: "artifact", artifact_id: item.artifact_id! },
          label: item.name,
          target,
        },
      })),
    ...Object.values(artifactsOnly ? {} : nodes).map((node): Entry => ({
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
    ...(!artifactsOnly && paper?.files.length
      ? [
          {
            folder: "paper" as const,
            reference: {
              selector: { kind: "paper" as const },
              label: "Whole paper",
              target,
            },
          },
          ...paper.files.map((file): Entry => ({
            folder: "paper",
            reference: {
              selector: { kind: "paper", file_id: file.file_id },
              label: file.path,
              target,
            },
          })),
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
  const folders = (Object.keys(FOLDER_LABEL) as FolderId[])
    .filter((id) => !artifactsOnly || (id !== "nodes" && id !== "paper"))
    .map((id) => ({
      id,
      count: entries.filter((entry) => entry.folder === id).length,
    }));
  // An open Reports or Artifacts folder groups like the Artifacts page, folds included.
  const groups =
    folder === "reports" || folder === "artifacts"
      ? branchGroups(shown, (entry) => entry.artifact!)
      : [];
  const grouped = groups.length > 1;
  const row = ({ folder: entryFolder, reference }: Entry) => {
    const added = selectedKeys.has(referenceKey(reference.selector));
    return (
      <li key={referenceKey(reference.selector)}>
        <button type="button" disabled={added} onClick={() => onPick(reference)}>
          <ReferenceIcon kind={reference.selector.kind} />
          <span>{reference.label}</span>
          {added ? (
            <Check size={14} aria-label="Added" />
          ) : (
            searching && !folder && entryFolder && <small>{FOLDER_LABEL[entryFolder]}</small>
          )}
        </button>
      </li>
    );
  };
  const back = () => {
    setFolder(null);
    setQuery("");
  };
  return (
    <section className={`chat-reference-picker${error ? " is-error" : ""}`} aria-label="From RCP">
      <div className="chat-reference-picker-heading">
        {folder ? (
          <button type="button" aria-label="Back to RCP" onClick={back}>
            <ArrowLeft size={14} />
          </button>
        ) : (
          <Search size={14} aria-hidden="true" />
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
        {error && (
          <button
            type="button"
            aria-label="Retry references"
            title={error}
            onClick={() => {
              setError(null);
              setLoading(true);
              setRetry((value) => value + 1);
            }}
          >
            <RotateCw size={14} />
          </button>
        )}
        <button type="button" aria-label="Close" onClick={onClose}>
          <X size={14} />
        </button>
      </div>
      {folder && <div className="chat-reference-path">RCP / {FOLDER_LABEL[folder]}</div>}
      {loading && <span role="status">Loading…</span>}
      <div className="chat-reference-list">
        <ul>
          {!searching &&
            !folder &&
            folders.map(({ id, count }) => (
              <li key={id}>
                <button type="button" disabled={count === 0} onClick={() => setFolder(id)}>
                  <Folder size={14} aria-hidden="true" />
                  <span>{FOLDER_LABEL[id]}</span>
                  <small>{count}</small>
                  <ChevronRight size={14} aria-hidden="true" />
                </button>
              </li>
            ))}
          {!grouped && shown.map(row)}
          {!loading && !error && searching && shown.length === 0 && (
            <li className="chat-reference-empty">No matches.</li>
          )}
        </ul>
        {grouped && (
          <BranchGroupList
            projectId={projectId}
            groups={groups}
            refs={null}
            renderItems={(items) => <ul>{items.map(row)}</ul>}
          />
        )}
      </div>
    </section>
  );
}
