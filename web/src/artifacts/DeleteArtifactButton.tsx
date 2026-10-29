import { useState } from "react";
import { api } from "../core/api";
import { errorMessage } from "../core/errors";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import { artifactsProfile } from "./artifactResources";
import { closeArtifactIfOpen } from "./artifactViewerModel";
import { ArmedDeleteButton } from "../ui/ArmedDeleteButton";

/** An artifact's Delete. A finished Delete closes the viewer only while it still shows
 * this artifact. */
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
  const [deleting, setDeleting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const remove = async () => {
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

  return (
    <ArmedDeleteButton
      label={label}
      className={className}
      iconSize={iconSize}
      busy={deleting}
      failure={failure}
      onConfirm={() => void remove()}
    />
  );
}
