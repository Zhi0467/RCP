import { parseProjectHash } from "../experiments/experimentBoardModel";
import { graphTargetFromHash } from "../core/graphTarget";
import { setReferenceDrag } from "../core/projectReferences";
import { CopyReferenceButton } from "../core/CopyReferenceButton";
import { useState, type MouseEvent } from "react";
import {
  ChevronRight,
  Download,
  ExternalLink,
  GitCommitHorizontal,
  MessageCircle,
  RefreshCw,
} from "lucide-react";
import { openArtifact } from "./artifactViewerModel";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import { useProjectResource } from "../projects/projectResources";
import { artifactsProfile } from "./artifactResources";
import { DeleteArtifactButton } from "./DeleteArtifactButton";
import { isDesktopRuntime, openDesktopArtifactPdf } from "../core/desktopRuntime";
import { StoredArtifactDownload } from "./StoredArtifactDownload";
import { EpisodeAuthor } from "../experiments/EpisodeRunControls";
import type { GraphRef, ProjectArtifact } from "../core/types";
import { BranchTag } from "../core/BranchTag";

export function Artifacts({
  projectId,
  graphRefs = null,
  nodeTitle,
  onOpenNode,
}: {
  projectId: string;
  /** The project's refs, so each artifact names the branch it came from. */
  graphRefs?: GraphRef[] | null;
  nodeTitle: (nodeId: string) => string | null;
  onOpenNode: (nodeId: string) => void;
}) {
  const { store } = useRefreshRuntime();
  const profile = artifactsProfile(projectId);
  const resource = useProjectResource(profile);
  const entries = resource.value ?? null;
  const loading = resource.loading;
  const [actionError, setError] = useState<string | null>(null);
  const error = actionError || (resource.error ? String(resource.error) : null);
  const groups = branchGroups(entries ?? []);
  const [folded, setFolded] = useState(() => readFolded(projectId));
  const fold = (key: string, closed: boolean) => {
    if (folded.has(key) === closed) return;
    const next = new Set(folded);
    if (closed) next.add(key);
    else next.delete(key);
    setFolded(next);
    try {
      localStorage.setItem(foldedStorageKey(projectId), JSON.stringify([...next]));
    } catch {
      // Folding still works for this visit when the device keeps no storage.
    }
  };

  const open = async (
    event: MouseEvent<HTMLAnchorElement | HTMLButtonElement>,
    entry: ProjectArtifact,
  ) => {
    if (entry.view === "pdf" && !isDesktopRuntime()) {
      event.preventDefault();
      return;
    }
    event.preventDefault();
    setError(null);
    try {
      if (!entry.artifact_id) throw new Error("This artifact has no stored viewer.");
      if (entry.view === "pdf") {
        await openDesktopArtifactPdf({
          projectId,
          artifactId: entry.artifact_id,
        });
      } else {
        openArtifact({ projectId, artifactId: entry.artifact_id });
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const row = (entry: ProjectArtifact, grouped: boolean) => {
    const sourceNodeTitle = entry.source_node_id ? nodeTitle(entry.source_node_id) : null;
    const target =
      parseProjectHash(entry.source_chat_href ?? "").experimentRoute?.graph_target ??
      graphTargetFromHash(entry.source_chat_href ?? "");
    return (
      <li
        key={entry.id}
        className="artifact-entry"
        draggable={Boolean(entry.artifact_id && entry.available)}
        onDragStart={(event) => {
          if (!entry.artifact_id || !entry.available) {
            event.preventDefault();
            return;
          }
          setReferenceDrag(event.dataTransfer, projectId, target, {
            kind: "artifact",
            artifact_id: entry.artifact_id,
          });
        }}
      >
        <div>
          <div className="artifact-entry-heading">
            <h3>{entry.name}</h3>
            <EpisodeAuthor author={entry.authorized_by} />
            {!grouped && (
              <BranchTag refs={graphRefs} target={target} name={entry.graph_branch_name} />
            )}
          </div>
          {entry.source_chat_href && (
            <a
              className="artifact-entry-chat"
              href={entry.source_chat_href}
              aria-label={`Open originating chat for ${entry.name}`}
            >
              <MessageCircle size={14} aria-hidden="true" /> Source chat
            </a>
          )}
          {entry.source_node_id && sourceNodeTitle !== null && (
            <button
              type="button"
              className="artifact-entry-chat artifact-entry-node"
              aria-label={`Open source node for ${entry.name}`}
              onClick={() => onOpenNode(entry.source_node_id!)}
            >
              <GitCommitHorizontal size={14} aria-hidden="true" /> {sourceNodeTitle}
            </button>
          )}
          {!entry.can_open && !entry.can_download && (
            <p>{entry.unavailable_reason || "Preview unavailable."}</p>
          )}
        </div>
        {!entry.can_open && entry.view === "pdf" && entry.can_download && isDesktopRuntime() && (
          <button
            className="button compact secondary artifact-entry-open"
            aria-label={`Open ${entry.name}`}
            onClick={(event) => void open(event, entry)}
          >
            <ExternalLink size={14} /> Open
          </button>
        )}
        {entry.artifact_id && entry.available && (
          <CopyReferenceButton
            projectId={projectId}
            graphTarget={graphTargetFromHash(entry.source_chat_href ?? "")}
            reference={{ kind: "artifact", artifact_id: entry.artifact_id }}
            className="button compact secondary artifact-entry-copy"
            showLabel
          />
        )}
        {entry.can_download && entry.download_url && entry.artifact_id && (
          <StoredArtifactDownload
            projectId={projectId}
            artifactId={entry.artifact_id}
            name={entry.name}
            className="button compact secondary artifact-entry-download"
            href={entry.download_url}
          >
            <Download size={14} /> Download
          </StoredArtifactDownload>
        )}
        {entry.can_open && entry.view !== "pdf" && (
          <a
            className="button compact secondary artifact-entry-open"
            href={entry.viewer_url ?? undefined}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`Open ${entry.name}`}
            onClick={(event) => void open(event, entry)}
          >
            <ExternalLink size={14} /> Open
          </a>
        )}
        {entry.can_delete && entry.artifact_id && (
          <DeleteArtifactButton
            projectId={projectId}
            artifactId={entry.artifact_id}
            label={`Delete ${entry.name}`}
            className="button compact secondary artifact-entry-delete"
            iconSize={14}
          />
        )}
      </li>
    );
  };

  return (
    <section
      className="view-panel artifacts-view"
      aria-label="Artifacts"
      style={error ? { border: "1px solid var(--danger, #b42318)" } : undefined}
    >
      <div className="view-heading artifacts-heading">
        <h2>Artifacts</h2>
        <button
          className="button compact secondary"
          disabled={loading && !error}
          aria-label="Refresh artifacts"
          title={error ?? "Refresh artifacts"}
          onClick={() => {
            setError(null);
            store.invalidate(resourceKeyId(profile.key));
          }}
        >
          <RefreshCw size={14} />
        </button>
      </div>
      {entries === null ? (
        loading && <p role="status">Loading artifacts…</p>
      ) : !error && entries.length === 0 ? (
        <p>No saved artifacts or reports yet.</p>
      ) : groups.length === 1 ? (
        <ul className="artifacts-list">{entries.map((entry) => row(entry, false))}</ul>
      ) : (
        groups.map((group) => (
          <details
            key={group.key}
            className="artifacts-branch"
            open={!folded.has(group.key)}
            onToggle={(event) => fold(group.key, !event.currentTarget.open)}
          >
            <summary>
              <ChevronRight className="artifacts-branch-chevron" size={14} aria-hidden="true" />
              <BranchTag
                refs={graphRefs}
                target={
                  group.key === MAIN ? { kind: "main" } : { kind: "branch", branch_id: group.key }
                }
                name={group.name}
              />
              <span className="artifacts-branch-count">{group.entries.length}</span>
            </summary>
            <ul className="artifacts-list">{group.entries.map((entry) => row(entry, true))}</ul>
          </details>
        ))
      )}
    </section>
  );
}

const MAIN = "main";

/** Entries under their branch, the branch with the newest artifact first. */
function branchGroups(entries: ProjectArtifact[]) {
  const groups = new Map<string, { key: string; name: string; entries: ProjectArtifact[] }>();
  for (const entry of entries) {
    const key = entry.graph_branch_id ?? MAIN;
    const group = groups.get(key) ?? { key, name: entry.graph_branch_name, entries: [] };
    group.entries.push(entry);
    groups.set(key, group);
  }
  return [...groups.values()];
}

function foldedStorageKey(projectId: string): string {
  return `rcp:artifacts-folded-branches:${projectId}`;
}

function readFolded(projectId: string): Set<string> {
  try {
    const keys: unknown = JSON.parse(localStorage.getItem(foldedStorageKey(projectId)) ?? "[]");
    return new Set(Array.isArray(keys) ? keys.filter((key) => typeof key === "string") : []);
  } catch {
    return new Set();
  }
}
