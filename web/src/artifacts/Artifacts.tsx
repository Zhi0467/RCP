import { parseProjectHash } from "../experiments/experimentBoardModel";
import { graphTargetFromHash } from "../core/graphTarget";
import { setReferenceDrag } from "../core/projectReferences";
import { CopyReferenceButton } from "../core/CopyReferenceButton";
import { useState, type MouseEvent } from "react";
import {
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
import { BranchGroupList, branchGroups } from "./BranchGroups";

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
  // Grouped as the server orders entries, so the branch with the newest output leads.
  const groups = branchGroups(entries ?? [], (entry) => entry);

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
          <h3>{entry.name}</h3>
          <div className="artifact-entry-meta">
            <EpisodeAuthor author={entry.authorized_by} />
            {!grouped && (
              <BranchTag refs={graphRefs} target={target} name={entry.graph_branch_name} />
            )}
            {entry.source_chat_href && (
              <a
                className="artifact-entry-chat"
                href={entry.source_chat_href}
                aria-label={`Open originating chat for ${entry.name}`}
              >
                <MessageCircle size={13} aria-hidden="true" /> Source chat
              </a>
            )}
            {entry.source_node_id && sourceNodeTitle !== null && (
              <button
                type="button"
                className="artifact-entry-chat artifact-entry-node"
                aria-label={`Open source node for ${entry.name}`}
                onClick={() => onOpenNode(entry.source_node_id!)}
              >
                <GitCommitHorizontal size={13} aria-hidden="true" /> {sourceNodeTitle}
              </button>
            )}
          </div>
          {!entry.can_open && !entry.can_download && (
            <p>{entry.unavailable_reason || "Preview unavailable."}</p>
          )}
        </div>
        {!entry.can_open && entry.view === "pdf" && entry.can_download && isDesktopRuntime() && (
          <button
            className="icon-button artifact-entry-action"
            aria-label={`Open ${entry.name}`}
            title="Open"
            onClick={(event) => void open(event, entry)}
          >
            <ExternalLink size={15} />
          </button>
        )}
        {entry.artifact_id && entry.available && (
          <CopyReferenceButton
            projectId={projectId}
            graphTarget={graphTargetFromHash(entry.source_chat_href ?? "")}
            reference={{ kind: "artifact", artifact_id: entry.artifact_id }}
            className="icon-button artifact-entry-action"
          />
        )}
        {entry.can_download && entry.download_url && entry.artifact_id && (
          <StoredArtifactDownload
            projectId={projectId}
            artifactId={entry.artifact_id}
            name={entry.name}
            className="icon-button artifact-entry-action"
            href={entry.download_url}
          >
            <Download size={15} />
          </StoredArtifactDownload>
        )}
        {entry.can_open && entry.view !== "pdf" && (
          // The whole row is this link; the actions above it stay clickable.
          <a
            className="artifact-entry-open"
            href={entry.viewer_url ?? undefined}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={`Open ${entry.name}`}
            onClick={(event) => void open(event, entry)}
          />
        )}
        {entry.can_delete && entry.artifact_id && (
          <DeleteArtifactButton
            projectId={projectId}
            artifactId={entry.artifact_id}
            label={`Delete ${entry.name}`}
            className="icon-button artifact-entry-action"
            iconSize={15}
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
        <BranchGroupList
          projectId={projectId}
          groups={groups}
          refs={graphRefs}
          renderItems={(items) => (
            <ul className="artifacts-list">{items.map((entry) => row(entry, true))}</ul>
          )}
        />
      )}
    </section>
  );
}
