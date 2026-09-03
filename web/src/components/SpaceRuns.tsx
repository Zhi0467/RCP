import { ChevronRight, FlaskConical, Telescope, WifiOff } from "lucide-react";
import { useMemo, type CSSProperties } from "react";
import { spaceRunRouteToken } from "../experimentBoard";
import type { SpaceRunIndexEntry, SpaceRunMode } from "../types";

interface Props {
  entries: SpaceRunIndexEntry[];
  onOpen: (projectId: string, experimentRoute?: string) => void;
}

export const SPACE_RUN_BADGE_PALETTE: Record<
  SpaceRunIndexEntry["health_tone"],
  { background: string; foreground: string }
> = {
  running: { background: "#dce9e5", foreground: "#245759" },
  waiting: { background: "#f4e7c1", foreground: "#604600" },
  degraded: { background: "#f5e5df", foreground: "#7d2e24" },
  stopping: { background: "#f4e7c1", foreground: "#604600" },
  stopped: { background: "#eee3d3", foreground: "#4d443c" },
  actionable: { background: "#f5e5df", foreground: "#7d2e24" },
  completed: { background: "#dce9e5", foreground: "#245759" },
};

export function SpaceRuns({ entries, onOpen }: Props) {
  const groups = useMemo(() => {
    const needsAction = entries.filter((entry) => entry.run_section === "needs_action");
    const completed = entries.filter((entry) => entry.run_section === "completed");
    return {
      needsAction,
      completed,
      completedByMode: [
        {
          mode: "experiment_loop" as const,
          title: "Experiment loop",
          entries: completed.filter((entry) => entry.mode === "experiment_loop"),
        },
        {
          mode: "auto_research" as const,
          title: "Auto-research",
          entries: completed.filter((entry) => entry.mode === "auto_research"),
        },
      ],
    };
  }, [entries]);

  return (
    <section className="space-runs" aria-labelledby="space-runs-title">
      <header className="space-runs-header">
        <h2 id="space-runs-title">Runs</h2>
        <span>{groups.needsAction.length} needs action</span>
      </header>

      <div className="space-runs-sections">
        <RunSection title="Needs Action" entries={groups.needsAction} onOpen={onOpen} />
        <section className="space-runs-completed" aria-label="Completed runs">
          <header>
            <h3>Completed</h3>
            <span>{groups.completed.length}</span>
          </header>
          {groups.completed.length === 0 ? (
            <p className="space-runs-empty">No completed runs in the last 7 days.</p>
          ) : (
            groups.completedByMode.map((group) => (
              <CompletedGroup {...group} onOpen={onOpen} key={group.mode} />
            ))
          )}
        </section>
      </div>
    </section>
  );
}

function RunSection({
  title,
  entries,
  onOpen,
}: {
  title: string;
  entries: SpaceRunIndexEntry[];
  onOpen: Props["onOpen"];
}) {
  return (
    <section className="space-runs-section" aria-label={title}>
      <header>
        <h3>{title}</h3>
        <span>{entries.length}</span>
      </header>
      {entries.length === 0 ? (
        <p className="space-runs-empty">Nothing needs action.</p>
      ) : (
        <RunRows entries={entries} onOpen={onOpen} />
      )}
    </section>
  );
}

function CompletedGroup({
  mode,
  title,
  entries,
  onOpen,
}: {
  mode: SpaceRunMode;
  title: string;
  entries: SpaceRunIndexEntry[];
  onOpen: Props["onOpen"];
}) {
  if (entries.length === 0) return null;
  return (
    <details className="space-runs-group">
      <summary>
        <span aria-hidden="true">
          {mode === "experiment_loop" ? <FlaskConical size={13} /> : <Telescope size={13} />}
        </span>
        <strong>{title}</strong>
        <span>{entries.length}</span>
        <ChevronRight className="space-runs-fold" size={14} aria-hidden="true" />
      </summary>
      <RunRows entries={entries} onOpen={onOpen} />
    </details>
  );
}

function RunRows({ entries, onOpen }: { entries: SpaceRunIndexEntry[]; onOpen: Props["onOpen"] }) {
  return (
    <ul className="space-runs-rows">
      {entries.map((entry) => (
        <SpaceRunRow entry={entry} onOpen={onOpen} key={entry.episode_id} />
      ))}
    </ul>
  );
}

export function SpaceRunRow({
  entry,
  onOpen,
}: {
  entry: SpaceRunIndexEntry;
  onOpen: Props["onOpen"];
}) {
  const badge = SPACE_RUN_BADGE_PALETTE[entry.health_tone];
  return (
    <li className={`space-run-row ${entry.health_tone}`}>
      <button
        type="button"
        onClick={() => {
          onOpen(entry.project_id, spaceRunRouteToken(entry));
        }}
      >
        <span className="space-run-rail" aria-hidden="true" />
        <span className="space-run-copy">
          <strong>{entry.title}</strong>
          <span>{entry.project_name}</span>
        </span>
        <span className="space-run-meta">
          <span
            className={`status-pill ${entry.health_tone}`}
            style={
              {
                backgroundColor: badge.background,
                color: badge.foreground,
              } as CSSProperties
            }
          >
            {entry.health_label}
          </span>
          {entry.project_reachable === false && (
            <span className="space-run-unavailable">
              <WifiOff size={11} aria-hidden="true" /> Unavailable
            </span>
          )}
          <time dateTime={entry.started_at}>{formatActivity(entry.started_at)}</time>
        </span>
        <ChevronRight className="space-run-arrow" size={15} aria-hidden="true" />
      </button>
    </li>
  );
}

function formatActivity(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "Activity time unavailable";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
