import { File } from "lucide-react";
import type { ChatAttachmentDescriptor } from "../core/types";
import { ReferenceChip } from "../core/ReferenceChip";
import { sourceReference } from "../core/projectReferences";
import { formatBytes } from "./composerAttachments";

/** The project references and files a sent message carried, under its text. */
export function SentAttachments({
  attachments,
  projectId,
  now,
}: {
  attachments: ChatAttachmentDescriptor[] | undefined;
  projectId: string;
  now: number;
}) {
  if (!attachments?.length) return null;
  return (
    <>
      {attachments.some((attachment) => attachment.reference) && (
        <div className="chat-input-references">
          {attachments.map((attachment) =>
            attachment.reference ? (
              <ReferenceChip
                key={attachment.attachment_id}
                projectId={projectId}
                reference={{
                  ...sourceReference(attachment.reference),
                  label: attachment.name,
                }}
                version={
                  attachment.reference.graph_head
                    ? `r${attachment.reference.graph_head.revision}`
                    : attachment.reference.version?.slice(0, 7)
                }
              />
            ) : null,
          )}
        </div>
      )}
      {attachments.map((attachment) => {
        if (attachment.reference) return null;
        const expired = Date.parse(attachment.expires_at) <= now;
        return (
          <div
            className={`chat-input-attachment${expired ? " expired" : ""}`}
            key={attachment.attachment_id}
          >
            <File size={14} />
            <span>
              <strong>{attachment.name}</strong>
              <small>
                {attachment.media_type} · {formatBytes(attachment.size)}
              </small>
            </span>
            {expired && <em>Expired</em>}
          </div>
        );
      })}
    </>
  );
}
