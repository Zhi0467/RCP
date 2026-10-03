import { useState } from "react";
import { Copy } from "lucide-react";
import { referenceUrl } from "../projectReferences";
import type { GraphTargetRef, ProjectReferenceSelector } from "../types";

export function CopyReferenceButton({
  projectId,
  graphTarget,
  reference,
  className = "icon-button",
}: {
  projectId: string;
  graphTarget: GraphTargetRef;
  reference: ProjectReferenceSelector;
  className?: string;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <>
      <button
        type="button"
        className={className}
        aria-label="Copy reference"
        title="Copy reference"
        onClick={async () => {
          setNotice(null);
          try {
            await navigator.clipboard.writeText(referenceUrl(projectId, graphTarget, reference));
          } catch {
            setNotice("Could not copy the reference. Check clipboard access and try again.");
          }
        }}
      >
        <Copy size={16} />
      </button>
      {notice && <span role="alert">{notice}</span>}
    </>
  );
}
