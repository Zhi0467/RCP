import { useEffect, useState, type MouseEvent } from "react";
import { ExternalLink, RefreshCw } from "lucide-react";
import { api } from "../api";
import {
  isDesktopRuntime,
  openDesktopArtifactPreview,
  openDesktopEpisodeReportPreview,
} from "../desktopRuntime";
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

  const open = async (event: MouseEvent<HTMLAnchorElement>, entry: ProjectArtifact) => {
    if (!isDesktopRuntime()) return;
    event.preventDefault();
    setError(null);
    try {
      if (entry.episode_id) {
        await openDesktopEpisodeReportPreview({ projectId, episodeId: entry.episode_id });
      } else if (entry.operation_id && entry.artifact_id) {
        await openDesktopArtifactPreview({
          projectId,
          taskId: entry.operation_id,
          artifactId: entry.artifact_id,
        });
      } else {
        throw new Error("This artifact cannot be opened in the desktop app.");
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
                <h3>{entry.name}</h3>
                <p className="artifact-entry-meta">
                  <span>{entry.kind === "report" ? "Episode report" : "Saved artifact"}</span>
                  <time dateTime={entry.created_at}>
                    {new Date(entry.created_at).toLocaleString()}
                  </time>
                </p>
                {entry.episode_id && (
                  <p className="artifact-entry-path">Episode {entry.episode_id}</p>
                )}
                {entry.path && <p className="artifact-entry-path">{entry.path}</p>}
                {!entry.can_open && <p>{entry.unavailable_reason || "Preview unavailable."}</p>}
              </div>
              {entry.can_open && (
                <a
                  className="button compact secondary"
                  href={entry.viewer_url}
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
