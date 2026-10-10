import { useLayoutEffect, useRef } from "react";
import { LoaderCircle, RefreshCw, TriangleAlert } from "lucide-react";
import { backendReconnectLabel } from "../core/desktopRuntime";

export function ReconnectOverlay({
  issue,
  reconnecting,
  desktop,
  onReconnect,
}: {
  issue: string | null;
  reconnecting: boolean;
  desktop: boolean;
  onReconnect: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const dialog = dialogRef.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="reconnect-dialog"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="reconnect-title"
      onCancel={(event) => event.preventDefault()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      {issue ? (
        <>
          <TriangleAlert aria-hidden="true" />
          <h2 id="reconnect-title">Reconnect to RCP</h2>
          <p>{issue}</p>
          <button className="button secondary" disabled={reconnecting} onClick={onReconnect}>
            {reconnecting ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}{" "}
            {backendReconnectLabel(desktop)}
          </button>
        </>
      ) : (
        <>
          <LoaderCircle className="spin" aria-hidden="true" />
          <h2 id="reconnect-title">Verifying your identity</h2>
        </>
      )}
    </dialog>
  );
}
