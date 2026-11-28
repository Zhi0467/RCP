import { FoldVertical, Settings2 } from "lucide-react";
import { createContext, useContext, useEffect } from "react";
import { formatTokens } from "./agentStatusModel";
import { formatIdle, type CompactOffer } from "./compactOffer";

/** Opens Space settings at Machines, where each provider's auto-compact limit is set. */
export const OpenMachineSettingsContext = createContext<(() => void) | null>(null);

/** Asks once, before a message to a large idle session, whether to compact it first. */
export function CompactOfferDialog({
  offer,
  onChoose,
  onCancel,
}: {
  offer: CompactOffer;
  onChoose: (compactFirst: boolean) => void;
  onCancel: () => void;
}) {
  const openMachineSettings = useContext(OpenMachineSettingsContext);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [onCancel]);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <section
        className="project-delete-dialog compact-offer-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="compact-offer-title"
      >
        <header>
          <FoldVertical size={20} aria-hidden="true" />
          <h2 id="compact-offer-title">Compact before sending?</h2>
        </header>
        <p>
          Idle {formatIdle(offer.idleMs)} · {formatTokens(offer.contextTokens)} context
        </p>
        <footer>
          {openMachineSettings && (
            <button
              type="button"
              className="button ghost compact compact-offer-limits"
              title="Set each provider's auto-compact limit on its machine"
              onClick={() => {
                onCancel();
                openMachineSettings();
              }}
            >
              <Settings2 size={14} aria-hidden="true" /> Auto-compact limits
            </button>
          )}
          <button className="button secondary" type="button" onClick={() => onChoose(false)}>
            Send as is
          </button>
          <button className="button primary" type="button" autoFocus onClick={() => onChoose(true)}>
            Compact and send
          </button>
        </footer>
      </section>
    </div>
  );
}
