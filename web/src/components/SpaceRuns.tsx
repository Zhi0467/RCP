import { ChevronRight, FlaskConical, Telescope, WifiOff } from "lucide-react";
import { useMemo, type CSSProperties } from "react";
import { spaceRunRouteToken } from "../experimentBoard";
import { useTheme } from "../hooks/useTheme";
import type { ResolvedTheme } from "../theme";
import type { SpaceRunIndexEntry, SpaceRunMode } from "../types";

interface Props {
  entries: SpaceRunIndexEntry[];
  onOpen: (projectId: string, experimentRoute?: string) => void;
}

/** Lifecycle badge colours, per painted theme.
 *
 * These are literal pairs rather than palette tokens because the contrast
 * guarantee is asserted on the exact values a reader sees; `contrastRatio`
 * cannot resolve a `var()` outside a browser.
 */
export const SPACE_RUN_BADGE_PALETTE: Record<
  ResolvedTheme,
  Record<SpaceRunIndexEntry["health_tone"], { background: string; foreground: string }>
> = {
  light: {
    running: { background: "#dce9e5", foreground: "#245759" },
    waiting: { background: "#f4e7c1", foreground: "#604600" },
    degraded: { background: "#f5e5df", foreground: "#7d2e24" },
    stopping: { background: "#f4e7c1", foreground: "#604600" },
    stopped: { background: "#eee3d3", foreground: "#4d443c" },
    actionable: { background: "#f5e5df", foreground: "#7d2e24" },
    completed: { background: "#dce9e5", foreground: "#245759" },
  },
  dark: {
    running: { background: "#1e3a38", foreground: "#8fd3cb" },
    waiting: { background: "#3a301a", foreground: "#e8c579" },
    degraded: { background: "#3d2521", foreground: "#f0a08c" },
    stopping: { background: "#3a301a", foreground: "#e8c579" },
    stopped: { background: "#2c2621", foreground: "#bdb2a2" },
    actionable: { background: "#3d2521", foreground: "#f0a08c" },
    completed: { background: "#1e3a38", foreground: "#8fd3cb" },
  },
};

export function SpaceRuns({ entries, onOpen }: Props) {
  const { resolved: theme } = useTheme();
  const groups = useMemo(() => {
    const needsAction = entries.filter((entry) => entry.run_section === "actionable");
    const inProgress = entries.filter((entry) => entry.run_section === "running");
    const completed = entries.filter((entry) => entry.run_section === "completed");
    return {
      needsAction,
      inProgress,
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
        <span>
          {groups.needsAction.length} needs action · {groups.inProgress.length} in progress
        </span>
      </header>

      <div className="space-runs-sections">
        <RunSection
          title="Needs action"
          empty="Nothing needs you right now."
          entries={groups.needsAction}
          theme={theme}
          onOpen={onOpen}
        />
        <RunSection
          title="In progress"
          empty="No run is in flight."
          entries={groups.inProgress}
          theme={theme}
          onOpen={onOpen}
        />
        <section className="space-runs-completed" aria-label="Completed runs">
          <header>
            <h3>Completed</h3>
            <span>{groups.completed.length}</span>
          </header>
          {groups.completed.length === 0 ? (
            <p className="space-runs-empty">No completed runs in the last 7 days.</p>
          ) : (
            groups.completedByMode.map((group) => (
              <CompletedGroup {...group} theme={theme} onOpen={onOpen} key={group.mode} />
            ))
          )}
        </section>
      </div>
    </section>
  );
}

function RunSection({
  title,
  empty,
  entries,
  theme,
  onOpen,
}: {
  title: string;
  empty: string;
  entries: SpaceRunIndexEntry[];
  theme: ResolvedTheme;
  onOpen: Props["onOpen"];
}) {
  return (
    <section className="space-runs-section" aria-label={title}>
      <header>
        <h3>{title}</h3>
        <span>{entries.length}</span>
      </header>
      {entries.length === 0 ? (
        <p className="space-runs-empty">{empty}</p>
      ) : (
        <RunRows entries={entries} theme={theme} onOpen={onOpen} />
      )}
    </section>
  );
}

function CompletedGroup({
  mode,
  title,
  entries,
  theme,
  onOpen,
}: {
  mode: SpaceRunMode;
  title: string;
  entries: SpaceRunIndexEntry[];
  theme: ResolvedTheme;
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
      <RunRows entries={entries} theme={theme} onOpen={onOpen} />
    </details>
  );
}

function RunRows({
  entries,
  theme,
  onOpen,
}: {
  entries: SpaceRunIndexEntry[];
  theme: ResolvedTheme;
  onOpen: Props["onOpen"];
}) {
  return (
    <ul className="space-runs-rows">
      {entries.map((entry) => (
        <SpaceRunRow entry={entry} theme={theme} onOpen={onOpen} key={entry.episode_id} />
      ))}
    </ul>
  );
}

export function SpaceRunRow({
  entry,
  theme,
  onOpen,
}: {
  entry: SpaceRunIndexEntry;
  theme: ResolvedTheme;
  onOpen: Props["onOpen"];
}) {
  const badge = SPACE_RUN_BADGE_PALETTE[theme][entry.health_tone];
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
