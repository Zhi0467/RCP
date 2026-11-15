import { useState } from "react";
import { Link } from "lucide-react";
import { copyShareLink, useShareLinks } from "./shareLinks";

/** Copies a team share link to one agent reply; nothing outside a team space. */
export function ReplyLinkButton({
  projectId,
  graphTarget,
  operationId,
}: {
  projectId: string;
  graphTarget: string;
  operationId: string;
}) {
  const [notice, setNotice] = useState<string | null>(null);
  const share = useShareLinks();
  if (!share) return null;
  return (
    <>
      <button
        type="button"
        className="chat-answer-annotation-button"
        aria-label="Copy link to this reply"
        title="Copy link"
        onClick={async () => {
          setNotice(null);
          try {
            await copyShareLink(projectId, {
              kind: "reply",
              item_id: operationId,
              graph_target: graphTarget,
            });
          } catch (cause) {
            setNotice(cause instanceof Error ? cause.message : String(cause));
          }
        }}
      >
        <Link size={12} /> Link
      </button>
      {notice && <span role="alert">{notice}</span>}
    </>
  );
}
