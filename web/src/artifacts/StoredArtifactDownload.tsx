import { useState } from "react";
import { downloadDesktopArtifact, isDesktopRuntime } from "../core/desktopRuntime";
import { errorMessage } from "../core/errors";

export function StoredArtifactDownload({
  projectId,
  artifactId,
  name,
  href,
  className,
  children,
}: {
  projectId: string;
  artifactId: string;
  name: string;
  href: string;
  className?: string;
  children: React.ReactNode;
}) {
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  if (!isDesktopRuntime())
    return (
      <a className={className} href={href} download={name} aria-label={`Download ${name}`}>
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
        onClick={() => {
          setError("");
          setSaving(true);
          void downloadDesktopArtifact({ projectId, artifactId, suggestedName: name })
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
