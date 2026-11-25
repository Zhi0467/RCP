import { useState } from "react";
import { downloadDesktopArtifact, isDesktopRuntime } from "../core/desktopRuntime";
import { errorMessage } from "../core/errors";

export function StoredArtifactDownload({
  projectId,
  artifactId,
  name,
  fileName,
  href,
  className,
  children,
}: {
  projectId: string;
  artifactId: string;
  name: string;
  /** The saved file's name with its extension; `name` may be a display title. */
  fileName: string;
  href: string;
  className?: string;
  children: React.ReactNode;
}) {
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  if (!isDesktopRuntime())
    return (
      <a
        className={className}
        href={href}
        download={fileName}
        aria-label={`Download ${name}`}
        title="Download"
      >
        {children}
      </a>
    );
  return (
    <>
      <button
        className={className}
        type="button"
        disabled={saving}
        aria-label={`Download ${name}`}
        title="Download"
        onClick={() => {
          setError("");
          setSaving(true);
          void downloadDesktopArtifact({ projectId, artifactId, suggestedName: fileName })
            .catch((failure) => setError(errorMessage(failure)))
            .finally(() => setSaving(false));
        }}
      >
        {children}
      </button>
      {error && <span role="alert">{error}</span>}
    </>
  );
}
