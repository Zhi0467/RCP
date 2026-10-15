import { useEffect, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { api } from "../core/api";
import { errorMessage } from "../core/errors";
import { artifactAlreadyGone, artifactDeleteUrl, DELETE_CONFIRM_MS } from "./artifactDelete";

const dangerEdge = { borderColor: "var(--danger, #b42318)" };

/** Two-click Delete: the first click arms it, the second sends the request. */
export function DeleteArtifactButton({
  projectId,
  artifactId,
  label,
  className,
  iconSize,
  onDeleted,
}: {
  projectId: string;
  artifactId: string;
  label: string;
  className: string;
  iconSize: number;
  onDeleted: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );
  useEffect(() => {
    if (!confirming) return;
    const timer = window.setTimeout(() => setConfirming(false), DELETE_CONFIRM_MS);
    return () => window.clearTimeout(timer);
  }, [confirming]);

  const click = async () => {
    if (deleting) return;
    if (!confirming) {
      setConfirming(true);
      return;
    }
    setConfirming(false);
    setFailure(null);
    setDeleting(true);
    try {
      await api(artifactDeleteUrl(projectId, artifactId), { method: "DELETE" });
      onDeleted();
    } catch (cause) {
      if (artifactAlreadyGone(cause)) onDeleted();
      else if (mounted.current) setFailure(errorMessage(cause));
    } finally {
      if (mounted.current) setDeleting(false);
    }
  };

  const name = confirming ? "Confirm delete" : label;
  return (
    <button
      type="button"
      className={`${className}${confirming ? " artifact-delete-confirm" : ""}`}
      aria-label={name}
      title={!confirming && failure ? failure : name}
      style={failure && !confirming ? dangerEdge : undefined}
      disabled={deleting}
      onBlur={() => setConfirming(false)}
      onClick={() => void click()}
    >
      <Trash2 size={iconSize} aria-hidden="true" />
      {confirming && "Delete"}
    </button>
  );
}
