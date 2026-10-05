import { BookOpen, File, Network, X } from "lucide-react";
import { referenceUrl, type DraftReference } from "./projectReferences";

const KIND_LABEL = { artifact: "Artifact", node: "Node", paper: "Paper" } as const;

export function ReferenceIcon({ kind }: { kind: DraftReference["selector"]["kind"] }) {
  const Icon = kind === "node" ? Network : kind === "paper" ? BookOpen : File;
  return <Icon size={14} aria-hidden="true" />;
}

/** One reference, in the composer (removable) or in a sent turn (frozen version). */
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
  const kind = reference.selector.kind;
  return (
    <div className={onRemove ? "chat-attachment-chip chat-reference-chip" : "chat-input-reference"}>
      <ReferenceIcon kind={kind} />
      <a href={referenceUrl(projectId, reference.target, reference.selector)}>
        <strong>{reference.label}</strong>
        <small>{version ? `${KIND_LABEL[kind]} · ${version}` : KIND_LABEL[kind]}</small>
      </a>
      {onRemove && (
        <button type="button" aria-label={`Remove ${reference.label}`} onClick={onRemove}>
          <X size={12} />
        </button>
      )}
    </div>
  );
}
