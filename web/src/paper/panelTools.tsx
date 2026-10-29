import "./panel-tools.css";
import type { ReactNode } from "react";
import { createPortal } from "react-dom";

/** Puts a Paper panel's actions in the header row beside the panel switch; `failed` gives them the red edge. */
export function panelTools(toolbar: HTMLElement | null, actions: ReactNode, failed = false) {
  return (
    toolbar &&
    createPortal(
      <div className={`paper-panel-tools${failed ? " paper-failure" : ""}`}>{actions}</div>,
      toolbar,
    )
  );
}
