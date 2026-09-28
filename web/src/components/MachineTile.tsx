import { Plus, Server } from "lucide-react";

export type MachineSignalTone = "ready" | "warning" | "error" | "pending";

export interface MachineSignal {
  label: string;
  tone: MachineSignalTone;
}

/** One machine as a small clickable card; used to pick or open a machine. */
export function MachineTile({
  name,
  hostLabel,
  account = "",
  signals = [],
  selected = false,
  disabled = false,
  onSelect,
}: {
  name: string;
  hostLabel: string;
  account?: string;
  signals?: MachineSignal[];
  selected?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      className={`machine-tile${selected ? " selected" : ""}`}
      type="button"
      data-machine-tile={name}
      aria-pressed={selected}
      disabled={disabled}
      onClick={onSelect}
    >
      <span className="machine-tile-icon" aria-hidden="true">
        <Server size={15} />
      </span>
      <strong>{name}</strong>
      <span className="machine-tile-host">
        {hostLabel}
        {account ? ` · ${account}` : ""}
      </span>
      {signals.length > 0 && (
        <span className="machine-tile-signals">
          {signals.map((signal) => (
            <span className={`machine-tile-signal ${signal.tone}`} key={signal.label}>
              <i aria-hidden="true" />
              {signal.label}
            </span>
          ))}
        </span>
      )}
    </button>
  );
}

/** The dashed tile that starts adding a machine. */
export function AddMachineTile({
  label,
  selected = false,
  disabled = false,
  onSelect,
}: {
  label: string;
  selected?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      className={`machine-tile add${selected ? " selected" : ""}`}
      type="button"
      data-machine-action="add-machine"
      aria-pressed={selected}
      disabled={disabled}
      onClick={onSelect}
    >
      <span className="machine-tile-icon" aria-hidden="true">
        <Plus size={15} />
      </span>
      <strong>{label}</strong>
    </button>
  );
}
