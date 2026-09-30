import { useEffect, useState, type MouseEvent } from "react";
import { Download, ExternalLink, MessageSquare, RefreshCw } from "lucide-react";
import { openArtifact } from "../artifactViewer";
import { api } from "../api";
import { isDesktopRuntime, openDesktopArtifactPdf } from "../desktopRuntime";
import { StoredArtifactDownload } from "../components/StoredArtifactDownload";
import type { ProjectArtifact } from "../types";

export function Artifacts({ projectId }: { projectId: string }) {
  const [entries, setEntries] = useState<ProjectArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void api<ProjectArtifact[]>(`/api/projects/${encodeURIComponent(projectId)}/artifacts`, {
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) setEntries(result);
      })
      .catch((cause) => {
        if (!controller.signal.aborted)
          setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [projectId, refresh]);

  useEffect(() => {
    const onFocus = () => setRefresh((value) => value + 1);
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, []);

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

  return (
    <section className="view-panel artifacts-view" aria-label="Artifacts">
      <div className="view-heading artifacts-heading">
        <h2>Artifacts</h2>
        <button
          className="button compact secondary"
          disabled={loading}
          onClick={() => setRefresh((value) => value + 1)}
        >
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
      {loading ? (
        <p role="status">Loading artifacts…</p>
      ) : !error && entries.length === 0 ? (
        <p>No saved artifacts or reports yet.</p>
      ) : (
        <ul className="artifacts-list">
          {entries.map((entry) => (
            <li key={entry.id} className="artifact-entry">
              <div>
                <div className="artifact-entry-heading">
                  <h3>{entry.name}</h3>
                  {entry.episode_mode && (
                    <span className="artifact-episode-tag">
                      {entry.episode_mode === "experiment_loop" ? "Experiment" : "Auto-research"}
                    </span>
                  )}
                </div>
                {entry.source_chat_href && (
                  <a
                    className="artifact-entry-chat"
                    href={entry.source_chat_href}
                    aria-label={`Open originating chat for ${entry.name}`}
                  >
                    <MessageSquare size={14} aria-hidden="true" /> Source chat
                  </a>
                )}
                {!entry.can_open && !entry.can_download && (
                  <p>{entry.unavailable_reason || "Preview unavailable."}</p>
                )}
              </div>
              {!entry.can_open &&
                entry.view === "pdf" &&
                entry.can_download &&
                isDesktopRuntime() && (
                  <button
                    className="button compact secondary artifact-entry-open"
                    aria-label={`Open ${entry.name}`}
                    onClick={(event) => void open(event, entry)}
                  >
                    <ExternalLink size={14} /> Open
                  </button>
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
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
