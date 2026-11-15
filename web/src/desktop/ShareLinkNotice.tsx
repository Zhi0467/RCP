import { Link2, Plus, RefreshCw, WifiOff, X } from "lucide-react";
import { useEffect, useState } from "react";
import {
  dismissShareLink,
  listenShareLinkUnpaired,
  readShareLinkNotice,
  retryShareLink,
  type ShareLinkNotice as Notice,
} from "../core/desktopRuntime";

export function ShareLinkNotice({ onAddTeamSpace }: { onAddTeamSpace: (host: string) => void }) {
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    let stopped = false;
    let unlisten: () => void = () => undefined;
    void listenShareLinkUnpaired((next) => {
      if (!stopped) setNotice(next);
    }).then((stop) => {
      if (stopped) stop();
      else unlisten = stop;
    });
    void readShareLinkNotice()
      .then((current) => {
        if (!stopped) setNotice(current);
      })
      .catch(() => undefined);
    return () => {
      stopped = true;
      unlisten();
    };
  }, []);

  if (!notice) return null;
  const unreachable = notice.state === "unreachable";
  return (
    <div
      className={`banner desktop-update-notice release-update-notice share-link-notice${unreachable ? " error" : ""}`}
      role={unreachable ? "alert" : "status"}
      title={
        unreachable ? "This team space could not be reached" : "This team space is not added here"
      }
      data-state={notice.state}
    >
      {unreachable ? <WifiOff size={16} /> : <Link2 size={16} />}
      <strong>{notice.host}</strong>
      {unreachable ? (
        <button
          className="button secondary"
          type="button"
          onClick={() => void retryShareLink().catch(() => undefined)}
        >
          <RefreshCw size={14} /> Retry
        </button>
      ) : (
        <button
          className="button secondary"
          type="button"
          onClick={() => onAddTeamSpace(notice.host)}
        >
          <Plus size={14} /> Add team space
        </button>
      )}
      <button
        className="banner-dismiss"
        type="button"
        aria-label="Dismiss"
        onClick={() => {
          setNotice(null);
          void dismissShareLink().catch(() => undefined);
        }}
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  );
}
