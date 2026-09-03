import { ChevronRight, FlaskConical, Telescope, WifiOff } from "lucide-react";
import { useMemo } from "react";
import { spaceRunRouteToken } from "../experimentBoard";
import type { SpaceRunIndexEntry, SpaceRunMode } from "../types";

interface Props {
  entries: SpaceRunIndexEntry[];
  onOpen: (projectId: string, experimentRoute?: string) => void;
}

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

      {entries.length === 0 ? (
        <div className="space-runs-empty">No runs yet</div>
      ) : (
        <div className="space-runs-sections">
          <RunSection title="Needs Action" entries={groups.needsAction} onOpen={onOpen} />
          {groups.completed.length > 0 && (
            <section className="space-runs-completed" aria-label="Completed runs">
              <header>
                <h3>Completed</h3>
                <span>{groups.completed.length}</span>
              </header>
              {groups.completedByMode.map((group) => (
                <CompletedGroup {...group} onOpen={onOpen} key={group.mode} />
              ))}
            </section>
          )}
        </div>
      )}
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
  if (entries.length === 0) return null;
  return (
    <section className="space-runs-section" aria-label={title}>
      <header>
        <h3>{title}</h3>
        <span>{entries.length}</span>
      </header>
      <RunRows entries={entries} onOpen={onOpen} />
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
        <li className={`space-run-row ${entry.health_tone}`} key={entry.episode_id}>
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
              <span className={`status-pill ${entry.health_tone}`}>{entry.health_label}</span>
              {entry.project_reachable === false && (
                <span className="space-run-unavailable">
                  <WifiOff size={11} aria-hidden="true" /> Unavailable
                </span>
              )}
              <time dateTime={entry.last_activity_at}>
                {formatActivity(entry.last_activity_at)}
              </time>
            </span>
            <ChevronRight className="space-run-arrow" size={15} aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
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
