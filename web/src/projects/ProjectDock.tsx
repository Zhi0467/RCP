import type { ProjectSyncHealth } from "../core/refreshScheduler";
import { X } from "lucide-react";
import type { ProjectTab } from "./projectTabs";

interface Props {
  tabs: ProjectTab[];
  activeProjectId: string | null;
  onActivate: (projectId: string) => void;
  onClose: (projectId: string) => void;
  className?: string;
  sync?: ReadonlyMap<string, ProjectSyncHealth>;
}

export function ProjectDock({
  tabs,
  activeProjectId,
  onActivate,
  onClose,
  className = "",
  sync,
}: Props) {
  if (tabs.length === 0) return null;
  return (
    <nav className={`project-dock ${className}`.trim()} aria-label="Open projects">
      <div className="project-dock-scroll">
        {tabs.map((tab) => {
          const active = tab.id === activeProjectId;
          const health = sync?.get(tab.id);
          const syncLabel =
            health && health.status !== "local"
              ? `${tab.name}: sync ${health.status}${health.last_completed_at ? `; last completed ${health.last_completed_at}` : ""}${health.error ? `; ${health.error}` : ""}`
              : null;
          return (
            <div className={`project-dock-tab${active ? " active" : ""}`} key={tab.id}>
              <button
                className="project-dock-select"
                type="button"
                aria-current={active ? "page" : undefined}
                title={tab.name}
                onClick={() => onActivate(tab.id)}
              >
                <span>{tab.name}</span>
                {syncLabel && (
                  <span
                    role="status"
                    aria-label={syncLabel}
                    title={syncLabel}
                    style={{
                      fontSize: 8,
                      color: health?.status === "error" ? "var(--danger, #b33)" : "currentColor",
                    }}
                  >
                    ●
                  </span>
                )}
              </button>
              <button
                className="project-dock-close"
                type="button"
                aria-label={`Close ${tab.name}`}
                title={`Close ${tab.name}`}
                onClick={() => onClose(tab.id)}
              >
                <X size={12} aria-hidden="true" />
              </button>
            </div>
          );
        })}
      </div>
    </nav>
  );
}
