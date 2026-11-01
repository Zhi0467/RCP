import { researchViewsProfile } from "../graph/researchViewResources";
import { CopyReferenceButton } from "../core/CopyReferenceButton";
import { graphTargetFromHash, projectViewHash } from "../core/graphTarget";
import type { GraphTargetRef } from "../core/types";
import { branchName } from "../core/branchNames";
import { GraphPicker } from "../graph/GraphPicker";
import { refsProfile } from "../graph/graphResources";
import { pickerMergeRequestBody, type BranchMergeRequest } from "../graph/mergePanel";
import { taskStatusProfile } from "../agents/taskResources";
import { useCallback, useEffect, useRef, useState } from "react";
import { MessageCircle, PanelRight, Pin, RefreshCw, X } from "lucide-react";
import { api } from "../core/api";
import {
  closeArtifactViewer,
  openArtifact,
  openEpisodeReport,
  publishDockedArtifact,
  useArtifactViewerHidden,
  useArtifactViewerTarget,
} from "./artifactViewerModel";
import {
  acceptsArtifactEditMessage,
  artifactVersionChanged,
  collapseViewer,
  toggleViewerFullscreen,
  parseViewerPlacement,
  type ViewerPlacement,
} from "./artifactViewerLayout";
import { artifactPopupTarget } from "./artifactViewerRequests";
import { parseProjectHash } from "../experiments/experimentBoardModel";
import { listenDesktopEvent } from "../core/desktopRuntime";
import { StoredArtifactDownload } from "./StoredArtifactDownload";
import { errorMessage } from "../core/errors";
import { useResource, useRefreshRuntime } from "../core/useResource";
import { resourceKeyId } from "../core/resourceStore";
import { artifactStateProfile } from "./artifactResources";
import { DeleteArtifactButton } from "./DeleteArtifactButton";
import { repositoryFilePreviewUrl } from "../core/repositoryFileLinks";
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

function writePlacement(next: ViewerPlacement) {
  try {
    localStorage.setItem(placementKey, JSON.stringify(next));
  } catch {
    /* Optional preference. */
  }
}

export function ArtifactViewer({ onMerge }: { onMerge?: BranchMergeRequest }) {
  const target = useArtifactViewerTarget();
  const hidden = useArtifactViewerHidden();
  const [placement, setPlacement] = useState(readPlacement);
  const { store } = useRefreshRuntime();
  const profile =
    target?.kind === "artifact" ? artifactStateProfile(target.projectId, target.artifactId) : null;
  const resource = useResource("artifact-viewer", profile);
  const state = resource.value;
  const refs = useResource(
    "artifact-pin-refs",
    target?.kind === "artifact" ? refsProfile(target.projectId) : null,
  );
  useResource(
    "artifact-merge-tasks",
    onMerge && target?.kind === "artifact" ? taskStatusProfile(target.projectId) : null,
  );
  const [selectedPinTarget, setSelectedPinTarget] = useState<GraphTargetRef | null>(null);
  const [error, setError] = useState("");
  const [pinFailed, setPinFailed] = useState(false);
  const [editing, setEditing] = useState(false);
  const [pinning, setPinning] = useState(false);
  const [undoing, setUndoing] = useState(false);
  const [keeping, setKeeping] = useState(false);
  const [keepFailure, setKeepFailure] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const iframe = useRef<HTMLIFrameElement>(null);
  const refresh = () => {
    if (profile) store.invalidate(resourceKeyId(profile.key));
  };
  const requestGeneration = useRef(0);
  const updatePlacement = (next: ViewerPlacement) => {
    setPlacement(next);
    writePlacement(next);
  };
  const restore = useCallback(() => {
    setPlacement((current) => {
      const next = collapseViewer(current, false);
      writePlacement(next);
      return next;
    });
  }, []);

  useEffect(() => {
    requestGeneration.current += 1;
    previousVersion.current = null;
    setError("");
    setEditing(false);
    setUndoing(false);
    setKeeping(false);
    setKeepFailure(null);
    setPinning(false);
    setPinFailed(false);
    setSelectedPinTarget(null);
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
  const previousVersion = useRef<string | null>(null);
  useEffect(() => {
    if (state && artifactVersionChanged(previousVersion.current, state.current_version))
      setReload((value) => value + 1);
    previousVersion.current = state?.current_version ?? null;
    setEditing(Boolean(state?.editing_operation_id));
  }, [state]);
  useEffect(() => {
    if (!target || target.kind !== "artifact") return;
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
      setEditing(true);
      store.invalidate(
        resourceKeyId(artifactStateProfile(target.projectId, target.artifactId).key),
      );
    };
    window.addEventListener("message", message);
    return () => window.removeEventListener("message", message);
  }, [target, store]);

  const title = !target
    ? null
    : target.kind === "repository"
      ? target.path.split("/").pop() || target.path
      : (state?.name ?? "Artifact");
  // A docked viewer is an item in the project's dock, which the workspace renders.
  useEffect(() => {
    publishDockedArtifact(collapsed && title ? { title, restore } : null);
  }, [collapsed, title, restore]);
  useEffect(() => () => publishDockedArtifact(null), []);

  if (!target || !title || collapsed || hidden) return null;
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
      refresh();
    } catch (failure) {
      if (generation === requestGeneration.current) setError(errorMessage(failure));
    } finally {
      if (generation === requestGeneration.current) setUndoing(false);
    }
  };
  const keep = async () => {
    if (target.kind !== "artifact") return;
    const generation = requestGeneration.current;
    setKeeping(true);
    setKeepFailure(null);
    try {
      await api(
        `/api/projects/${encodeURIComponent(target.projectId)}/artifacts/${encodeURIComponent(target.artifactId)}/keep`,
        { method: "POST" },
      );
      if (generation !== requestGeneration.current) return;
      refresh();
    } catch (failure) {
      if (generation === requestGeneration.current) setKeepFailure(errorMessage(failure));
    } finally {
      if (generation === requestGeneration.current) setKeeping(false);
    }
  };
  const pinTarget: GraphTargetRef =
    selectedPinTarget ??
    (state?.pin_branch_id ? { kind: "branch", branch_id: state.pin_branch_id } : { kind: "main" });
  const pinBranchId = pinTarget.kind === "branch" ? pinTarget.branch_id : null;
  const pinned = state?.pinned_branch_ids.includes(pinBranchId ?? "") ?? false;
  const pinName = branchName(refs.value, pinTarget);
  const pinLabel = pinned ? `Open pin on ${pinName}` : `Pin in Artifacts on ${pinName}`;
  const pin = async () => {
    if (target.kind !== "artifact" || !state?.can_pin || pinning) return;
    if (pinned) {
      const pinHash = projectViewHash(target.projectId, pinTarget, "artifacts", {
        pinId: target.artifactId,
      });
      // Assigning the current hash fires no event, yet the pin may be unselected.
      if (window.location.hash === pinHash) window.dispatchEvent(new HashChangeEvent("hashchange"));
      else window.location.hash = pinHash;
      closeArtifactViewer();
      return;
    }
    const generation = requestGeneration.current;
    setPinning(true);
    try {
      await api(`/api/projects/${encodeURIComponent(target.projectId)}/research-views`, {
        method: "POST",
        body: JSON.stringify({ artifact_id: target.artifactId, branch_id: pinBranchId }),
      });
      store.invalidate(resourceKeyId(researchViewsProfile(target.projectId, pinBranchId).key));
      store.invalidate(
        resourceKeyId(artifactStateProfile(target.projectId, target.artifactId).key),
      );
      if (generation === requestGeneration.current) setPinFailed(false);
    } catch {
      if (generation === requestGeneration.current) setPinFailed(true);
    } finally {
      if (generation === requestGeneration.current) setPinning(false);
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
        style={
          error || resource.error ? { borderBottom: "2px solid var(--danger, #b42318)" } : undefined
        }
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
        {Boolean(error || resource.error) && (
          <button
            className="artifact-viewer-control"
            aria-label="Retry artifact"
            title={error || errorMessage(resource.error)}
            onClick={() => {
              setError("");
              refresh();
            }}
          >
            <RefreshCw size={16} />
          </button>
        )}
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
        {state?.can_pin && (
          <>
            <button
              className="artifact-viewer-control pin-view-action"
              aria-label={pinLabel}
              title={pinLabel}
              aria-pressed={pinned}
              style={pinFailed && !pinned ? { borderColor: "var(--danger, #b42318)" } : undefined}
              disabled={pinning}
              onClick={() => void pin()}
            >
              <Pin size={16} />
            </button>
            <GraphPicker
              compact
              projectId={target.projectId}
              refs={refs.value ?? (refs.error ? null : undefined)}
              activeRef={pinTarget}
              unavailable={Boolean(refs.error) || pinning}
              onSelect={(next) => {
                setSelectedPinTarget(next);
                setPinFailed(false);
              }}
              onRefUpdated={() =>
                store.invalidate(resourceKeyId(refsProfile(target.projectId).key))
              }
              onMerge={
                onMerge
                  ? (source, requestId, confirmUnfinishedJobs) =>
                      onMerge(
                        source,
                        pickerMergeRequestBody(confirmUnfinishedJobs),
                        requestId,
                        target.projectId,
                      )
                  : undefined
              }
            />
          </>
        )}
        {editing && <span role="status">Editing</span>}
        {state?.can_keep && (
          <button
            disabled={keeping}
            className={keepFailure ? "artifact-action-failed" : undefined}
            title={keepFailure ?? undefined}
            onClick={() => void keep()}
          >
            Keep
          </button>
        )}
        {state?.can_undo && (
          <button disabled={undoing} onClick={() => void undo()}>
            Undo
          </button>
        )}
        {state?.thread_href && (
          <a className="artifact-viewer-control" href={state.thread_href} aria-label="Source chat">
            <MessageCircle size={16} />
            <span className="artifact-viewer-label">Source chat</span>
          </a>
        )}
        {target.kind === "artifact" && state?.can_delete && (
          <DeleteArtifactButton
            key={target.artifactId}
            projectId={target.projectId}
            artifactId={target.artifactId}
            label="Delete artifact"
            className="artifact-viewer-control"
            iconSize={16}
          />
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
