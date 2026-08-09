import { X } from "lucide-react";
import { useLayoutEffect, useRef } from "react";
import type { ProjectTab } from "../projectTabs";

interface Props {
  tabs: ProjectTab[];
  activeProjectId: string | null;
  onActivate: (projectId: string) => void;
  onClose: (projectId: string) => void;
  className?: string;
}

export function ProjectDock({ tabs, activeProjectId, onActivate, onClose, className = "" }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const activeRef = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const scroller = scrollRef.current;
    const active = activeRef.current;
    if (!scroller || !active) return;
    const left = active.offsetLeft;
    const right = left + active.offsetWidth;
    if (left < scroller.scrollLeft) scroller.scrollLeft = left;
    else if (right > scroller.scrollLeft + scroller.clientWidth) {
      scroller.scrollLeft = right - scroller.clientWidth;
    }
  }, [activeProjectId, tabs]);

  if (tabs.length === 0) return null;
  return (
    <nav className={`project-dock ${className}`.trim()} aria-label="Open projects">
      <div className="project-dock-scroll" ref={scrollRef}>
        {tabs.map((tab) => {
          const active = tab.id === activeProjectId;
          return (
            <div
              className={`project-dock-tab${active ? " active" : ""}`}
              key={tab.id}
              ref={active ? activeRef : undefined}
            >
              <button
                className="project-dock-select"
                type="button"
                aria-current={active ? "page" : undefined}
                title={tab.name}
                onClick={() => onActivate(tab.id)}
              >
                <span>{tab.name}</span>
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
