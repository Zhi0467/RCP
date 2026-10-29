import type { ReactNode } from "react";
import { PaperDialog } from "./PaperDialog";

/** A short modal question: its label, then its buttons in one row; a failure marks the edge. */
export function PaperConfirm({
  label,
  failed = false,
  onClose,
  children,
}: {
  label: string;
  failed?: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <PaperDialog
      className={`paper-box paper-confirm${failed ? " paper-failed" : ""}`}
      role="alertdialog"
      label={label}
      onClose={onClose}
    >
      <strong>{label}</strong>
      <div className="paper-bar">{children}</div>
    </PaperDialog>
  );
}
