import { MAIN_GRAPH } from "../core/graphTarget";
import { setReferenceDrag } from "../core/projectReferences";
import type { GraphTargetRef } from "../core/types";
import { RotateCw } from "lucide-react";
import { artifactDownloadOnly, openArtifact } from "../artifacts/artifactViewerModel";
import type { RunArtifactEntry } from "../core/types";
import { ArtifactDownload, storedArtifactSave } from "../artifacts/ArtifactDownload";
import "../styles/runArtifacts.css";

export function RunArtifacts({
  projectId,
  graphTarget = MAIN_GRAPH,
  artifacts,
  error,
  loading,
  onRetry,
}: {
  projectId: string;
  graphTarget?: GraphTargetRef;
  artifacts: readonly RunArtifactEntry[];
  error?: string | null;
  loading?: boolean;
  onRetry?: () => void;
}) {
  if (!artifacts.length && !error && !loading) return null;
  return (
    <div className="run-artifacts" aria-busy={loading || undefined}>
      {/* A failed read is one red retry mark; its reason is the hover. */}
      {error && onRetry ? (
        <button
          type="button"
          className="icon-button run-artifacts-retry"
          aria-label={`Run artifacts failed: ${error}. Retry`}
          title={error}
          onClick={onRetry}
        >
          <RotateCw size={14} />
        </button>
      ) : error ? (
        <div className="campaign-run-error" role="alert">
          {error}
        </div>
      ) : null}
      {artifacts.length > 0 && (
        <ul aria-label="Run artifacts">
          {artifacts.map((artifact) => (
            <li
              key={artifact.artifact_id}
              draggable
              onDragStart={(event) =>
                setReferenceDrag(event.dataTransfer, projectId, graphTarget, {
                  kind: "artifact",
                  artifact_id: artifact.artifact_id,
                })
              }
            >
              {artifactDownloadOnly(artifact.view) ? (
                <>
                  <span>{artifact.name}</span>
                  <ArtifactDownload
                    save={storedArtifactSave(
                      projectId,
                      artifact.artifact_id,
                      artifact.download_name,
                    )}
                    name={artifact.name}
                    fileName={artifact.download_name}
                    className="button compact secondary"
                    href={`/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}/download`}
                  >
                    Download
                  </ArtifactDownload>
                </>
              ) : (
                <button
                  type="button"
                  className="run-artifact-open"
                  onClick={() => {
                    openArtifact({ projectId, artifactId: artifact.artifact_id });
                  }}
                >
                  {artifact.name}
                </button>
              )}
              <span>{artifact.supplier === "episode_ending" ? "Report" : artifact.view}</span>
              {artifact.worker_label && <span>{artifact.worker_label}</span>}
              <time dateTime={artifact.created_at}>
                {new Date(artifact.created_at).toLocaleString(undefined, {
                  month: "short",
                  day: "numeric",
                  hour: "numeric",
                  minute: "2-digit",
                })}
              </time>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
