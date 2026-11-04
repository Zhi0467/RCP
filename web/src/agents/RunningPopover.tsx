import { Activity, ChevronRight } from "lucide-react";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import type { RunningList, RunningRow, RunningRowKind } from "../core/types";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { useRefreshRuntime } from "../core/useResource";
import { retryBackoffMs } from "../core/resourceStore";
import { useMinuteClock } from "../core/useMinuteClock";
import { sinceLabel } from "./agentThreadModel";
import { loadRunningList } from "./agentThreadsApi";

const KIND_LABEL: Record<RunningRowKind, string> = {
  experiment_loop: "Experiment",
  auto_research: "Auto-research",
  thread: "Your agent",
};

const RUNNING_READ_GAP_MS = 2000;

/**
 * The Running list, read once and again whenever the open-card version moves
 * from a known value. Reads are at least RUNNING_READ_GAP_MS apart, so a burst of
 * moves costs one read. A version counts as read only once its list loaded; a failed
 * read keeps the last list and retries with backoff.
 */
function useRunningList(): RunningList | null {
  const { scheduler } = useRefreshRuntime();
  const [list, setList] = useState<RunningList | null>(null);
  useEffect(() => {
    let stopped = false;
    let loading = false;
    let failures = 0;
    let lastRead = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let wanted: string | null = null;
    let read: string | null | undefined;
    const stale = () => read === undefined || read !== wanted;
    const later = (ms: number) => {
      if (stopped || loading || timer !== null || !stale()) return;
      timer = setTimeout(load, Math.max(0, ms));
    };
    const load = () => {
      timer = null;
      loading = true;
      const version = wanted;
      lastRead = Date.now();
      loadRunningList().then(
        (next) => {
          loading = false;
          if (stopped) return;
          failures = 0;
          read = version;
          setList(next);
          later(RUNNING_READ_GAP_MS);
        },
        () => {
          loading = false;
          failures += 1;
          later(retryBackoffMs(failures));
        },
      );
    };
    const unwatch = scheduler.watchAgentCards((version) => {
      if (read === null && wanted === null) read = version;
      wanted = version;
      later(lastRead + RUNNING_READ_GAP_MS - Date.now());
    });
    later(0);
    return () => {
      stopped = true;
      if (timer !== null) clearTimeout(timer);
      unwatch();
    };
  }, [scheduler]);
  return list;
}

function RunningEntry({ row, now, onOpen }: { row: RunningRow; now: number; onOpen: () => void }) {
  const watching = row.watching > 0 && !/watching/i.test(row.label);
  return (
    <a className="upcoming-row running-row" href={row.link} onClick={onOpen}>
      <strong title={row.title}>{row.title}</strong>
      <span className="upcoming-when">
        {row.project_name} · {KIND_LABEL[row.kind]}
      </span>
      <span
        className={row.needs_you ? "upcoming-pending" : "upcoming-when"}
        data-deferred={row.needs_you ? "" : undefined}
      >
        <span>
          {row.label}
          {watching && ` · watching ${row.watching}`}
        </span>
        {row.at && <time dateTime={row.at}>{sinceLabel(row.at, now)}</time>}
      </span>
      <span className="upcoming-actions" aria-hidden="true">
        <ChevronRight size={14} />
      </span>
    </a>
  );
}

/**
 * The Space thread's Running button: what needs the member over everything
 * running in their projects. Hidden when nothing runs; a row only opens its run.
 */
export function RunningPopover() {
  const list = useRunningList();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useLayerDismiss(open, close, [trigger, panel], trigger);
  const now = useMinuteClock(open);
  if (!list?.total) return null;
  const firstWaiting = list.rows.findIndex((row) => !row.needs_you);
  return (
    <>
      <button
        ref={trigger}
        className="upcoming-button running-button"
        type="button"
        title="Running"
        aria-label={`Running, ${list.needs} of ${list.total} need you`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Activity size={14} aria-hidden="true" />
        {list.needs}
        <span className="running-total"> / {list.total}</span>
      </button>
      {open && (
        <div className="upcoming-popover" role="dialog" aria-label="Running" ref={panel}>
          <h3>Running</h3>
          {list.rows.map((row, index) => (
            <Fragment key={`${row.kind}:${row.id}`}>
              {index > 0 && index === firstWaiting && (
                <div className="running-divider" aria-hidden="true" />
              )}
              <RunningEntry row={row} now={now} onOpen={close} />
            </Fragment>
          ))}
        </div>
      )}
    </>
  );
}
