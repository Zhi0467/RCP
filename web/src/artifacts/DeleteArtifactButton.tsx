import { useState } from "react";
import { Trash2 } from "lucide-react";
import { api } from "../core/api";
import { errorMessage } from "../core/errors";
import { resourceKeyId } from "../core/resourceStore";
import { useArmedConfirm } from "../core/useArmedConfirm";
import { useRefreshRuntime } from "../core/useResource";
import { artifactsProfile } from "./artifactResources";
import { closeArtifactIfOpen } from "./artifactViewerModel";

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
  const { armed: confirming, arm, disarm } = useArmedConfirm();
  const [deleting, setDeleting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const click = async () => {
    if (!confirming) {
      arm();
      return;
    }
    disarm();
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
  const state = confirming ? " armed-delete" : failure ? " artifact-action-failed" : "";
  return (
    <button
      type="button"
      className={className + state}
      aria-label={name}
      title={!confirming && failure ? failure : name}
      disabled={deleting}
      onBlur={disarm}
      onClick={() => void click()}
    >
      <Trash2 size={iconSize} aria-hidden="true" />
      {confirming && "Delete"}
    </button>
  );
}
