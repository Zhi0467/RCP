import { useState } from "react";
import { downloadDesktopArtifact, isDesktopRuntime } from "../core/desktopRuntime";
import { errorMessage } from "../core/errors";

/** Saves a stored artifact through the desktop dialog; null in a browser. */
export function storedArtifactSave(
  projectId: string,
  artifactId: string,
  fileName: string,
): (() => Promise<unknown>) | null {
  return isDesktopRuntime()
    ? () => downloadDesktopArtifact({ projectId, artifactId, suggestedName: fileName })
    : null;
}

/** Every artifact Download: a plain link in a browser, the native save dialog on desktop. */
export function ArtifactDownload({
  name,
  fileName,
  href,
  save,
  className,
  children,
}: {
  name: string;
  /** The saved file's name with its extension; `name` may be a display title. */
  fileName: string;
  href: string;
  save: (() => Promise<unknown>) | null;
  className?: string;
  children: React.ReactNode;
}) {
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  if (!save)
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
          void save()
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
