import { ExternalLink, MessageSquare, RotateCw, X } from "lucide-react";
import { useRef, useState } from "react";
import type { EvidenceFlag, GraphNode } from "../core/types";
import { addressEvidenceFlag, dismissEvidenceFlag } from "../core/api";
import { openRepositoryFile } from "../artifacts/artifactViewerModel";
import type { EvidenceFlagAction, EvidenceFlagRow } from "./evidenceFlags";

interface Props {
  apiBase: string;
  projectId: string;
  rows: EvidenceFlagRow[];
  writesDisabled: boolean;
  onChanged: (flag: EvidenceFlag) => void;
  onOpenNode: (node: GraphNode) => void;
  onOpenChat: (chatId: string) => void;
}

const KIND_LABELS = { overclaim: "Overclaim", underclaim: "Underclaim", no_source: "No source" };

export function EvidenceFlagInbox(props: Props) {
  if (!props.rows.length) return null;
  return (
    <section className="consolidation-inbox" aria-label="Evidence to check">
      <header className="rail-heading proposal-section-heading">
        <h2>Evidence to check</h2>
        <span className="count-badge">{props.rows.length}</span>
      </header>
      {props.rows.map((row) => (
        <FlagRow key={row.flag.flag_id} {...props} row={row} />
      ))}
    </section>
  );
}

function FlagRow({
  row,
  apiBase,
  projectId,
  writesDisabled,
  onChanged,
  onOpenNode,
  onOpenChat,
}: Props & { row: EvidenceFlagRow }) {
  const { flag, node, title, source, actions } = row;
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ action: EvidenceFlagAction; message: string } | null>(
    null,
  );
  const inFlight = useRef(false);
  // Retain the request across an ambiguous transport failure so Retry cannot start another turn.
  const requestId = useRef<string | null>(null);
  const run = async (action: EvidenceFlagAction) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setFailure(null);
    try {
      if (action === "address") {
        requestId.current ??= crypto.randomUUID();
        const result = await addressEvidenceFlag(apiBase, flag.flag_id, requestId.current);
        onChanged(result.flag);
        onOpenChat(result.chat_id);
      } else if (action === "dismiss") {
        onChanged((await dismissEvidenceFlag(apiBase, flag.flag_id)).flag);
      } else if (action === "open_chat") {
        if (flag.chat_id) onOpenChat(flag.chat_id);
      } else if (action === "open_evidence" && node) {
        onOpenNode(node);
      } else if (action === "open_source" && source) {
        openRepositoryFile({ projectId, ...source });
      }
    } catch (error) {
      setFailure({ action, message: error instanceof Error ? error.message : String(error) });
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  return (
    <article
      className={`consolidation-row evidence-flag-row${failure ? " has-error" : ""}`}
      data-flag-id={flag.flag_id}
    >
      <div className="proposal-topline">
        <span className="eyebrow">{KIND_LABELS[flag.kind]}</span>
        <span className="mono">{flag.occurrence_date}</span>
      </div>
      <h3>{title}</h3>
      <p className="evidence-flag-finding">{flag.finding}</p>
      <div className="card-actions">
        {actions.map((action) => {
          const mutates = action === "address" || action === "dismiss";
          if (action === failure?.action)
            return (
              <button
                key={action}
                type="button"
                className="icon-button"
                aria-label="Retry action"
                title={failure.message}
                disabled={busy || (mutates && writesDisabled)}
                onClick={() => void run(action)}
              >
                <RotateCw size={16} />
              </button>
            );
          const label = {
            address: "Address",
            open_chat: "Open chat",
            open_evidence: "Open Evidence",
            open_source: "Open source",
            dismiss: "Dismiss",
          }[action];
          return (
            <button
              key={action}
              type="button"
              data-action={action}
              className={action === "dismiss" ? "icon-button" : "button secondary"}
              aria-label={label}
              title={action === "dismiss" ? label : undefined}
              disabled={
                busy || (mutates && writesDisabled) || (action === "open_chat" && !flag.chat_id)
              }
              onClick={() => void run(action)}
            >
              {action === "dismiss" ? (
                <X size={16} />
              ) : (
                <>
                  {action === "address" || action === "open_chat" ? (
                    <MessageSquare size={14} />
                  ) : (
                    <ExternalLink size={14} />
                  )}
                  {label}
                </>
              )}
            </button>
          );
        })}
      </div>
    </article>
  );
}
