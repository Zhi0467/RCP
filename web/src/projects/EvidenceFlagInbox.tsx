import { ExternalLink, LoaderCircle, MessageSquare, RotateCw, X } from "lucide-react";
import { useRef, useState } from "react";
import type { EvidenceFlag, GraphNode } from "../core/types";
import {
  ApiError,
  addressEvidenceFlag,
  addressEvidenceFlags,
  dismissEvidenceFlag,
} from "../core/api";
import { openRepositoryFile } from "../artifacts/artifactViewerModel";
import {
  evidenceFlagsToAddress,
  type EvidenceFlagAction,
  type EvidenceFlagRow,
} from "./evidenceFlags";

interface Props {
  apiBase: string;
  projectId: string;
  rows: EvidenceFlagRow[];
  writesDisabled: boolean;
  onChanged: (flag: EvidenceFlag) => void;
  onOpenNode: (node: GraphNode) => void;
  onOpenChat: (chatId: string) => void;
}

const KIND_LABELS: Record<EvidenceFlag["kind"], string> = {
  overclaim: "Overclaim",
  underclaim: "Underclaim",
  no_source: "No source",
  unverified: "Unverified",
  consolidation_added: "Added by consolidation",
};

export function EvidenceFlagInbox(props: Props) {
  if (!props.rows.length) return null;
  const addressable = evidenceFlagsToAddress(props.rows);
  return (
    <section className="consolidation-inbox" aria-label="Evidence to check">
      <header className="rail-heading proposal-section-heading evidence-flag-heading">
        <h2>Evidence to check</h2>
        <span className="count-badge">{props.rows.length}</span>
        {addressable.length > 1 && <AddressAll {...props} flags={addressable} />}
      </header>
      {props.rows.map((row) => (
        <FlagRow key={row.flag.flag_id} {...props} row={row} />
      ))}
    </section>
  );
}

/** One batch retains both its ids and request key across an ambiguous failure. */
function AddressAll({
  apiBase,
  flags,
  writesDisabled,
  onChanged,
}: Props & { flags: EvidenceFlag[] }) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const pending = useRef<{ id: string; flagIds: string[] } | null>(null);
  const inFlight = useRef(false);
  const run = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setFailure(null);
    pending.current ??= { id: crypto.randomUUID(), flagIds: flags.map((flag) => flag.flag_id) };
    try {
      const result = await addressEvidenceFlags(
        apiBase,
        pending.current.flagIds,
        pending.current.id,
      );
      result.flags.forEach(onChanged);
      pending.current = null;
    } catch (error) {
      // A refusal started nothing, so Retry picks a fresh batch; only an ambiguous failure replays.
      if (error instanceof ApiError && error.status < 500) pending.current = null;
      setFailure(error instanceof Error ? error.message : String(error));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  if (failure)
    return (
      <button
        type="button"
        className="icon-button evidence-flag-address-all has-error"
        aria-label="Retry Address all"
        title={failure}
        disabled={busy || writesDisabled}
        onClick={() => void run()}
      >
        <RotateCw size={16} />
      </button>
    );
  return (
    <button
      type="button"
      className="button secondary evidence-flag-address-all"
      disabled={busy || writesDisabled}
      onClick={() => void run()}
    >
      {busy ? <LoaderCircle size={14} className="spin" aria-hidden /> : <MessageSquare size={14} />}
      Address all
    </button>
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
        // The turn is bound; a later Address after it fails is a new request.
        requestId.current = null;
      } else if (action === "dismiss") {
        onChanged((await dismissEvidenceFlag(apiBase, flag.flag_id)).flag);
      } else if (action === "open_chat") {
        if (flag.chat_id) onOpenChat(flag.chat_id);
      } else if (action === "open_evidence" && node) {
        onOpenNode(node);
      } else if (action === "open_source" && source?.kind === "file") {
        openRepositoryFile({ projectId, ...source.target });
      }
    } catch (error) {
      setFailure({ action, message: error instanceof Error ? error.message : String(error) });
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  if (flag.state === "addressing")
    return (
      <article
        className="consolidation-row evidence-flag-row addressing"
        data-flag-id={flag.flag_id}
      >
        <LoaderCircle size={15} className="spin" aria-hidden />
        <span className="evidence-flag-strip-text">
          Agent addressing <span className="eyebrow">{KIND_LABELS[flag.kind]}</span>
        </span>
        <span className="evidence-flag-strip-title">{title}</span>
        <button
          type="button"
          className="icon-button"
          data-action="open_chat"
          aria-label="Open chat"
          title="Open chat"
          disabled={busy}
          onClick={() => void run("open_chat")}
        >
          <MessageSquare size={16} />
        </button>
      </article>
    );
  // An open flag that keeps a chat is back from a failed Address turn.
  const attemptFailed = flag.state === "open" && Boolean(flag.chat_id);
  return (
    <article
      className={`consolidation-row evidence-flag-row${failure || attemptFailed ? " has-error" : ""}`}
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
          if (action === failure?.action || (action === "address" && attemptFailed))
            return (
              <button
                key={action}
                type="button"
                className="icon-button"
                aria-label="Retry action"
                title={action === failure?.action ? failure.message : "Address again"}
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
          if (action === "open_source" && source?.kind === "url")
            return (
              <a
                key={action}
                data-action={action}
                className="button secondary"
                href={source.href}
                target="_blank"
                rel="noreferrer"
              >
                <ExternalLink size={14} />
                {label}
              </a>
            );
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
