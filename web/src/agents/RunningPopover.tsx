import { Activity, ChevronRight } from "lucide-react";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import type { RunningList, RunningRow, RunningRowKind } from "../core/types";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { useMinuteClock } from "../core/useMinuteClock";
import { sinceLabel } from "./agentThreadModel";
import { loadRunningList } from "./agentThreadsApi";
import { useAgentCardsVersion } from "./useAgentThreads";

const KIND_LABEL: Record<RunningRowKind, string> = {
  experiment: "Experiment",
  auto_research: "Auto-research",
  thread: "Your agent",
};

/**
 * The Running list, read once and again whenever the open-card version moves
 * from a known value. Only the newest read counts; a failed read keeps the last list.
 */
function useRunningList(): RunningList | null {
  const version = useAgentCardsVersion();
  const [list, setList] = useState<RunningList | null>(null);
  const latest = useRef(0);
  const shown = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const first = shown.current === undefined;
    const moved = shown.current != null && shown.current !== version;
    shown.current = version;
    if (!first && !moved) return;
    const mine = ++latest.current;
    loadRunningList().then(
      (next) => {
        if (mine === latest.current) setList(next);
      },
      () => undefined,
    );
  }, [version]);
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
        <time dateTime={row.at}>{sinceLabel(row.at, now)}</time>
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
