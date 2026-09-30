import { useState } from "react";
import { isDesktopRuntime, openDesktopArtifactPdf } from "../desktopRuntime";
import { errorMessage } from "../errors";
import { openArtifact } from "../artifactViewer";
import type { RunArtifactEntry } from "../types";
import "../styles/runArtifacts.css";

export function RunArtifacts({
  projectId,
  artifacts,
  error,
  loading,
  onRetry,
}: {
  projectId: string;
  artifacts: readonly RunArtifactEntry[];
  error?: string | null;
  loading?: boolean;
  onRetry?: () => void;
}) {
  const [openError, setOpenError] = useState<string | null>(null);
  if (!artifacts.length && !error && !loading) return null;
  return (
    <div className="run-artifacts">
      {openError && <div role="alert">{openError}</div>}
      {loading && <span role="status">Loading artifacts…</span>}
      {error && (
        <div className="campaign-run-error" role="alert">
          {error}{" "}
          {onRetry && (
            <button type="button" className="button compact" onClick={onRetry}>
              Retry
            </button>
          )}
        </div>
      )}
      {artifacts.length > 0 && (
        <ul aria-label="Run artifacts">
          {artifacts.map((artifact) => (
            <li key={artifact.artifact_id}>
              {artifact.view === "file" || (artifact.view === "pdf" && !isDesktopRuntime()) ? (
                <>
                  <span>{artifact.name}</span>
                  <a
                    className="button compact secondary"
                    href={`/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}/download`}
                    download={artifact.name}
                    aria-label={`Download ${artifact.name}`}
                  >
                    Download
                  </a>
                </>
              ) : (
                <button
                  type="button"
                  className="run-artifact-open"
                  onClick={() => {
                    setOpenError(null);
                    if (artifact.view === "pdf") {
                      if (!artifact.origin_operation_id) {
                        setOpenError("The PDF has no producing operation.");
                        return;
                      }
                      void openDesktopArtifactPdf({
                        projectId,
                        taskId: artifact.origin_operation_id,
                        artifactId: artifact.artifact_id,
                      }).catch((failure) => setOpenError(errorMessage(failure)));
                    } else openArtifact({ projectId, artifactId: artifact.artifact_id });
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
