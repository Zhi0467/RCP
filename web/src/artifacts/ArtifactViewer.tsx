import { CopyReferenceButton } from "../core/CopyReferenceButton";
import { graphTargetFromHash } from "../core/graphTarget";
import { useEffect, useRef, useState } from "react";
import { MessageCircle, PanelRight, X } from "lucide-react";
import { api, ApiError } from "../core/api";
import {
  announceArtifactVersionChange,
  closeArtifactViewer,
  openArtifact,
  openEpisodeReport,
  useArtifactViewerTarget,
  type ArtifactViewerTarget,
} from "./artifactViewerModel";
import {
  acceptsArtifactEditMessage,
  artifactVersionChanged,
  collapseViewer,
  toggleViewerFullscreen,
  parseViewerPlacement,
  type ViewerPlacement,
} from "./artifactViewerLayout";
import { artifactPopupTarget, isPermanentArtifactError } from "./artifactViewerRequests";
import { parseProjectHash } from "../experiments/experimentBoardModel";
import { listenDesktopEvent } from "../core/desktopRuntime";
import { StoredArtifactDownload } from "./StoredArtifactDownload";
import { errorMessage } from "../core/errors";
import { startLiveEpisodePolling } from "../experiments/useEpisodeDialogs";
import { repositoryFilePreviewUrl } from "../core/repositoryFileLinks";
import type { ArtifactViewerState } from "../core/types";
import { DraggableWindow } from "../ui/DraggableWindow";
import "../styles/artifact-viewer.css";

const placementKey = "rcp:artifact-viewer-placement";
function readPlacement() {
  try {
    return parseViewerPlacement(localStorage.getItem(placementKey));
  } catch {
    return parseViewerPlacement(null);
  }
}

export function ArtifactViewer() {
  const target = useArtifactViewerTarget();
  const [placement, setPlacement] = useState(readPlacement);
  const [loaded, setLoaded] = useState<{
    target: ArtifactViewerTarget;
    state: ArtifactViewerState;
  } | null>(null);
  const state = loaded?.target === target ? loaded.state : null;
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [undoing, setUndoing] = useState(false);
  const [reload, setReload] = useState(0);
  const iframe = useRef<HTMLIFrameElement>(null);
  const refresh = useRef<() => Promise<void>>(async () => {});
  const requestGeneration = useRef(0);
  const updatePlacement = (next: ViewerPlacement) => {
    setPlacement(next);
    try {
      localStorage.setItem(placementKey, JSON.stringify(next));
    } catch {
      /* Optional preference. */
    }
  };

  useEffect(() => {
    setLoaded(null);
    setError("");
    setEditing(false);
    setUndoing(false);
    if (target) setPlacement((current) => collapseViewer(current, false));
  }, [target]);

  useEffect(() => {
    let disposed = false;
    const unlisten = listenDesktopEvent<string>("rcp://open-artifact", async (url) => {
      if (disposed) return;
      const popup = artifactPopupTarget(
        url,
        window.location.origin,
        parseProjectHash(window.location.hash).projectId,
      );
      if (!popup) {
        console.warn("[rcp] dropped unrecognised artifact popup", url);
        return;
      }
      try {
        if (popup.kind === "artifact") openArtifact(popup);
        else await openEpisodeReport(popup);
      } catch (failure) {
        setError(errorMessage(failure));
      }
    });
    return () => {
      disposed = true;
      void unlisten.then((stop) => stop());
    };
  }, []);

  const collapsed = placement.collapsed;
  useEffect(() => {
    const generation = ++requestGeneration.current;
    if (!target || target.kind !== "artifact" || collapsed) return;
    let disposed = false;
    let permanentError = false;
    const controller = new AbortController();
    let pending: Promise<void> | null = null;
    let latest: ArtifactViewerState | null = null;
    let editStarted = false;
    let editSignal = 0;
    const url = `/api/projects/${encodeURIComponent(target.projectId)}/artifacts/${encodeURIComponent(target.artifactId)}`;
    const load = (): Promise<void> => {
      if (disposed || permanentError || document.visibilityState !== "visible")
        return Promise.resolve();
      if (pending) return pending;
      const signalAtStart = editSignal;
      pending = api<ArtifactViewerState>(`${url}/state`, { signal: controller.signal })
        .then((next) => {
          if (disposed || generation !== requestGeneration.current) return;
          if (artifactVersionChanged(latest?.current_version ?? null, next.current_version)) {
            setReload((value) => value + 1);
            announceArtifactVersionChange(target.artifactId);
          }
          latest = next;
          if (signalAtStart === editSignal) editStarted = Boolean(next.editing_operation_id);
          setLoaded({ target, state: next });
          setEditing(editStarted);
          setError("");
        })
        .catch((failure) => {
          if (disposed) return;
          if (failure instanceof ApiError)
            permanentError = isPermanentArtifactError(failure.status);
          throw failure;
        })
        .finally(() => {
          pending = null;
        });
      return pending;
    };
    refresh.current = async () => {
      await pending?.catch(() => {});
      permanentError = false;
      await load();
    };
    const showError = (failure: unknown) => {
      if (!disposed) setError(errorMessage(failure));
    };
    const visible = () => {
      void load().catch(showError);
    };
    const message = (event: MessageEvent) => {
      if (
        !acceptsArtifactEditMessage(
          event,
          iframe.current?.contentWindow,
          window.location.origin,
          target.artifactId,
        )
      )
        return;
      editSignal += 1;
      editStarted = true;
      setEditing(true);
      // The following poll observes publication even when the shell's Send response
      // arrived while a previous state request was still in flight.
    };
    window.addEventListener("message", message);
    document.addEventListener("visibilitychange", visible);
    visible();
    const stop = startLiveEpisodePolling(window, load, showError, () => {});
    return () => {
      disposed = true;
      controller.abort();
      stop();
      window.removeEventListener("message", message);
      document.removeEventListener("visibilitychange", visible);
      // The shell's own Keep changes nothing an inline copy watches, so the copies
      // reread their state when the panel lets go of the artifact.
      announceArtifactVersionChange(target.artifactId);
    };
  }, [target, collapsed]);

  if (!target) return null;
  const title =
    target.kind === "repository"
      ? target.path.split("/").pop() || target.path
      : (state?.name ?? "Artifact");
  if (collapsed)
    return (
      <button
        className="artifact-viewer-tab"
        onClick={() => updatePlacement(collapseViewer(placement, false))}
        aria-label={`Restore ${title}`}
      >
        {title}
      </button>
    );
  const source =
    target.kind === "repository"
      ? repositoryFilePreviewUrl(target.projectId, { path: target.path, line: target.line ?? null })
      : state?.viewer_url;
  const undo = async () => {
    if (target.kind !== "artifact" || undoing) return;
    const generation = requestGeneration.current;
    setUndoing(true);
    try {
      await api(
        `/api/projects/${encodeURIComponent(target.projectId)}/artifacts/${encodeURIComponent(target.artifactId)}/undo`,
        { method: "POST" },
      );
      if (generation !== requestGeneration.current) return;
      await refresh.current();
      if (generation !== requestGeneration.current) return;
      setReload((value) => value + 1);
      announceArtifactVersionChange(target.artifactId);
    } catch (failure) {
      if (generation === requestGeneration.current) setError(errorMessage(failure));
    } finally {
      if (generation === requestGeneration.current) setUndoing(false);
    }
  };
  return (
    <DraggableWindow
      className="artifact-viewer"
      kind="detail"
      viewer={{ placement, onChange: updatePlacement }}
      focusRequestToken={target.kind === "artifact" ? target.artifactId : target.path}
    >
      <header
        className="artifact-viewer-header"
        data-drag-handle
        tabIndex={0}
        aria-label="Artifact title bar"
        aria-keyshortcuts="Enter Space"
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget || !["Enter", " "].includes(event.key)) return;
          event.preventDefault();
          updatePlacement(toggleViewerFullscreen(placement));
        }}
      >
        <strong title={title}>{title}</strong>
        {state?.live && (
          <span className={`artifact-viewer-status ${state.live}`}>
            {state.live === "live" ? "Live" : "Finished"}
          </span>
        )}
        {state && <span>v{state.version_number}</span>}
        {target.kind === "artifact" && state && !error && (
          <CopyReferenceButton
            projectId={target.projectId}
            graphTarget={
              parseProjectHash(state?.thread_href ?? "").experimentRoute?.graph_target ??
              graphTargetFromHash(state?.thread_href ?? window.location.hash)
            }
            reference={{ kind: "artifact", artifact_id: target.artifactId }}
            className="artifact-viewer-control"
          />
        )}
        {editing && <span role="status">Editing</span>}
        {state?.can_undo && (
          <button disabled={undoing} onClick={() => void undo()}>
            Undo
          </button>
        )}
        {state?.thread_href && (
          <a className="artifact-viewer-control" href={state.thread_href}>
            <MessageCircle size={16} />
            Source chat
          </a>
        )}
        <button
          className="artifact-viewer-control"
          onClick={() => updatePlacement(collapseViewer(placement, true))}
          aria-label="Dock viewer"
          title="Dock viewer"
        >
          <PanelRight size={16} />
        </button>
        <button
          className="artifact-viewer-control"
          onClick={closeArtifactViewer}
          aria-label="Close viewer"
          title="Close viewer"
        >
          <X size={16} />
        </button>
      </header>
      {error && (
        <div className="artifact-viewer-error" role="alert">
          {error}
          <button
            onClick={() =>
              void refresh.current().catch((failure) => setError(errorMessage(failure)))
            }
          >
            Retry
          </button>
        </div>
      )}
      {state?.can_comment && (
        <div className="artifact-viewer-comment-guidance">
          Comments edit the artifact; use the source chat for analysis or code.
        </div>
      )}
      {source ? (
        <iframe
          ref={iframe}
          key={`${target.kind === "artifact" ? target.artifactId : target.path}:${reload}`}
          src={source}
          title={title}
        />
      ) : state ? (
        <StoredArtifactDownload
          key={state.artifact_id}
          projectId={target.projectId}
          artifactId={state.artifact_id}
          name={state.name}
          className="artifact-viewer-file"
          href={state.download_url}
        >
          Download {title}
        </StoredArtifactDownload>
      ) : (
        <div className="artifact-viewer-loading" role="status">
          Loading…
        </div>
      )}
    </DraggableWindow>
  );
}
