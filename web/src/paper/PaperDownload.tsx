import type { ReactNode } from "react";
import { ArtifactDownload } from "../artifacts/ArtifactDownload";
import { storedPaperSave, type StoredPaperItem } from "./paperDownloadModel";

/**
 * Every Paper Download link: a plain link in a browser, the native save dialog
 * on desktop when the bytes are a stored file.
 */
export function PaperDownload({
  projectId,
  stored,
  href,
  name,
  fileName,
  className,
  children,
}: {
  projectId: string;
  /** The stored file behind `href`; null for bytes the backend holds no file for. */
  stored: StoredPaperItem | null;
  href: string;
  name: string;
  fileName: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <ArtifactDownload
      name={name}
      fileName={fileName}
      href={href}
      save={storedPaperSave(projectId, stored)}
      className={className}
    >
      {children}
    </ArtifactDownload>
  );
}
