import { useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import { api } from "../core/api";
import { errorMessage } from "../core/errors";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import { artifactsProfile } from "./artifactResources";
import { closeArtifactIfOpen } from "./artifactViewerModel";

/** A first click arms Delete; this long without a second click disarms it. */
const CONFIRM_MS = 4000;

/**
 * Two-click Delete: the first click arms it, the second sends the request. A
 * finished Delete closes the viewer only while it still shows this artifact.
 */
export function DeleteArtifactButton({
  projectId,
  artifactId,
  label,
  className,
  iconSize,
}: {
  projectId: string;
  artifactId: string;
  label: string;
  className: string;
  iconSize: number;
}) {
  const { store } = useRefreshRuntime();
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  useEffect(() => {
    if (!confirming) return;
    const timer = window.setTimeout(() => setConfirming(false), CONFIRM_MS);
    return () => window.clearTimeout(timer);
  }, [confirming]);

  const click = async () => {
    if (!confirming) {
      setConfirming(true);
      return;
    }
    setConfirming(false);
    setFailure(null);
    setDeleting(true);
    try {
      // The route answers 204 again for an artifact that is already gone.
      await api(
        `/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}`,
        { method: "DELETE" },
      );
      closeArtifactIfOpen(artifactId);
      store.invalidate(resourceKeyId(artifactsProfile(projectId).key));
    } catch (cause) {
      setFailure(errorMessage(cause));
    } finally {
      setDeleting(false);
    }
  };

  const name = confirming ? "Confirm delete" : label;
  const state = confirming ? " artifact-delete-confirm" : failure ? " artifact-action-failed" : "";
  return (
    <button
      type="button"
      className={className + state}
      aria-label={name}
      title={!confirming && failure ? failure : name}
      disabled={deleting}
      onBlur={() => setConfirming(false)}
      onClick={() => void click()}
    >
      <Trash2 size={iconSize} aria-hidden="true" />
      {confirming && "Delete"}
    </button>
  );
}
