import { useEffect, useRef, type ReactNode } from "react";

/** Native modality owns focus containment and Escape; unmount restores the opener. */
export function PaperDialog({
  children,
  label,
  className = "paper-inspector",
  role,
  onClose,
}: {
  children: ReactNode;
  label: string;
  className?: string;
  role?: "alertdialog";
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current!;
    element.showModal();
    return () => {
      element.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className={`paper-dialog ${className}`}
      aria-label={label}
      role={role}
      onCancel={(event) => {
        event.preventDefault();
        // React bubbles a nested dialog's cancel to its parent dialog; close only this one.
        event.stopPropagation();
        onClose();
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        if (
          event.clientX < bounds.left ||
          event.clientX > bounds.right ||
          event.clientY < bounds.top ||
          event.clientY > bounds.bottom
        )
          onClose();
      }}
    >
      {children}
    </dialog>
  );
}
