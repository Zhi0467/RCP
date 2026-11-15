import { Link2, Plus, RefreshCw, WifiOff } from "lucide-react";
import { useEffect, useState } from "react";
import {
  dismissShareLink,
  listenShareLinkUnpaired,
  readShareLinkNotice,
  retryShareLink,
  type ShareLinkNotice as Notice,
} from "../core/desktopRuntime";
import { Banner } from "../ui/Banner";

export function ShareLinkNotice({ onAddTeamSpace }: { onAddTeamSpace: () => void }) {
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    let stopped = false;
    let unlisten: () => void = () => undefined;
    let heard = false;
    void listenShareLinkUnpaired((next) => {
      heard = true;
      if (!stopped) setNotice(next);
    })
      .then((stop) => {
        if (stopped) {
          stop();
          return;
        }
        unlisten = stop;
        return readShareLinkNotice().then((current) => {
          if (!stopped && !heard) setNotice(current);
        });
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
    <Banner
      className={`desktop-update-notice release-update-notice share-link-notice${unreachable ? " error" : ""}`}
      role={unreachable ? "alert" : "status"}
      title={
        unreachable ? "This team space could not be reached" : "This team space is not added here"
      }
      data-state={notice.state}
      onDismiss={() => {
        setNotice(null);
        void dismissShareLink().catch(() => undefined);
      }}
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
        <button className="button secondary" type="button" onClick={onAddTeamSpace}>
          <Plus size={14} /> Add team space
        </button>
      )}
    </Banner>
  );
}
