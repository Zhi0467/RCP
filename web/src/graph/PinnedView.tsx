import { useEffect, useRef, useState } from "react";
import { PinOff, RefreshCw } from "lucide-react";
import { api } from "../core/api";
import { errorMessage } from "../core/errors";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime, useResource } from "../core/useResource";
import type { ResearchViewPin } from "../core/types";
import { artifactStateProfile } from "../artifacts/artifactResources";
import { acceptsArtifactOpenNodeMessage } from "../artifacts/artifactViewerLayout";
import { researchViewsProfile } from "./researchViewResources";

export function PinnedView({
  projectId,
  branchId,
  pin,
  ready,
  nodes,
  onOpenNode,
}: {
  projectId: string;
  branchId: string | null;
  pin: ResearchViewPin;
  ready: boolean;
  nodes: Record<string, unknown>;
  onOpenNode: (id: string) => void;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const { store } = useRefreshRuntime();
  const profile = artifactStateProfile(projectId, pin.artifact_id);
  const resource = useResource("pinned-research-view", profile);
  const state = resource.value;
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (
        !acceptsArtifactOpenNodeMessage(
          event,
          frame.current?.contentWindow,
          window.location.origin,
          pin.artifact_id,
          ready && state?.artifact_id === pin.artifact_id && state.pin_branch_id === branchId,
          nodes,
        )
      )
        return;
      onOpenNode(event.data.node_id);
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [ready, state, branchId, pin.artifact_id, nodes, onOpenNode]);
  return (
    <div
      className={`pinned-research-view${resource.error ? " failed" : ""}`}
      role="tabpanel"
      aria-label={pin.title}
    >
      {Boolean(resource.error) && (
        <button
          className="pinned-view-retry"
          aria-label="Retry view"
          title={errorMessage(resource.error)}
          onClick={() => store.invalidate(resourceKeyId(profile.key))}
        >
          <RefreshCw size={16} />
        </button>
      )}
      {state && (
        <iframe
          ref={frame}
          key={state.current_version}
          title={pin.title}
          src={`/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(pin.artifact_id)}/viewer`}
        />
      )}
    </div>
  );
}

export function PinnedViewTab({
  projectId,
  branchId,
  pin,
  active,
  onSelect,
}: {
  projectId: string;
  branchId: string | null;
  pin: ResearchViewPin;
  active: boolean;
  onSelect: () => void;
}) {
  const { store } = useRefreshRuntime();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const unpin = async () => {
    setBusy(true);
    try {
      await api(
        `/api/projects/${encodeURIComponent(projectId)}/research-views/${encodeURIComponent(pin.artifact_id)}?branch_id=${encodeURIComponent(branchId ?? "")}`,
        { method: "DELETE" },
      );
      store.invalidate(resourceKeyId(researchViewsProfile(projectId, branchId).key));
      store.invalidate(resourceKeyId(artifactStateProfile(projectId, pin.artifact_id).key));
      setError("");
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={`research-pinned-tab${error ? " failed" : ""}`} role="presentation">
      <button
        role="tab"
        aria-selected={active}
        className={active ? "active" : ""}
        title={pin.title}
        onClick={onSelect}
      >
        <span>{pin.title}</span>
      </button>
      <button
        className="research-unpin"
        disabled={busy}
        aria-label={error ? `Retry unpin ${pin.title}` : `Unpin ${pin.title}`}
        title={error || `Unpin ${pin.title}`}
        onClick={() => void unpin()}
      >
        {error ? <RefreshCw size={14} /> : <PinOff size={14} />}
      </button>
    </div>
  );
}
