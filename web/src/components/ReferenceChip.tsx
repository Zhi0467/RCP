import { BookOpen, File, Network, X } from "lucide-react";
import { referenceUrl, type DraftReference } from "../projectReferences";

export function ReferenceChip({
  projectId,
  reference,
  version,
  onRemove,
}: {
  projectId: string;
  reference: DraftReference;
  version?: string;
  onRemove?: () => void;
}) {
  const Icon =
    reference.selector.kind === "node"
      ? Network
      : reference.selector.kind === "paper"
        ? BookOpen
        : File;
  return (
    <div className="chat-attachment-chip chat-reference-chip">
      <Icon size={13} />
      <a href={referenceUrl(projectId, reference.target, reference.selector)}>
        {reference.label}
        {version && <small>{version}</small>}
      </a>
      {onRemove && (
        <button type="button" aria-label={`Remove ${reference.label}`} onClick={onRemove}>
          <X size={12} />
        </button>
      )}
    </div>
  );
}
