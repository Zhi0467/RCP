import { useState } from "react";
import { Copy, Link } from "lucide-react";
import { referenceUrl } from "./projectReferences";
import { copyShareLink, shareLinkRequest, useShareLinks } from "./shareLinks";
import type { GraphTargetRef, ProjectReferenceSelector } from "./types";

/** Copy reference, or Copy link in a team space that hands out share links. */
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
  const share = useShareLinks();
  const label = share ? "Copy link" : "Copy reference";
  return (
    <>
      <button
        type="button"
        className={className}
        aria-label={label}
        title={label}
        onClick={async () => {
          setNotice(null);
          if (share) {
            try {
              await copyShareLink(projectId, shareLinkRequest(graphTarget, reference));
            } catch (cause) {
              setNotice(cause instanceof Error ? cause.message : String(cause));
            }
            return;
          }
          try {
            await navigator.clipboard.writeText(referenceUrl(projectId, graphTarget, reference));
          } catch {
            setNotice("Could not copy the reference. Check clipboard access and try again.");
          }
        }}
      >
        {share ? <Link size={16} /> : <Copy size={16} />}
      </button>
      {notice && <span role="alert">{notice}</span>}
    </>
  );
}
