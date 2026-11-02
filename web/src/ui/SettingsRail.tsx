import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import "./SettingsRail.css";

export interface SettingsSection {
  id: string;
  label: string;
  icon: ReactNode;
  content: ReactNode;
}

interface SettingsRailProps {
  /** Only the sections that exist. */
  sections: SettingsSection[];
  /** Absent, unknown, or missing: the first section. */
  initialSection?: string;
  /** Page-level footer below the pane, such as a save bar. */
  children?: ReactNode;
}

/**
 * A settings page's section list beside one pane at a time. The selection
 * lives here and nowhere else, so leaving the page starts the next visit
 * fresh. Every pane stays mounted, so drafts and loaded data survive a switch.
 */
export function SettingsRail({ sections, initialSection, children }: SettingsRailProps) {
  const [chosen, setChosen] = useState(initialSection);
  const selected = sections.find((section) => section.id === chosen) ?? sections[0];
  const rootRef = useRef<HTMLDivElement>(null);
  const railRef = useRef<HTMLElement>(null);

  // On a phone the rail is a strip that scrolls in its own width; keep the
  // selected item inside it without scrolling the page.
  useLayoutEffect(() => {
    const rail = railRef.current;
    const item = rail?.querySelector<HTMLElement>('[aria-current="true"]');
    if (!rail || !item || rail.scrollWidth <= rail.clientWidth) return;
    const left = item.getBoundingClientRect().left - rail.getBoundingClientRect().left;
    const right = left + item.offsetWidth;
    if (left < 0) rail.scrollLeft += left;
    else if (right > rail.clientWidth) rail.scrollLeft += right - rail.clientWidth;
  }, [selected?.id]);

  const choose = (id: string) => {
    if (id === selected?.id) return;
    setChosen(id);
    // A new section starts at the top of the page, whichever ancestor scrolls.
    for (let node = rootRef.current?.parentElement; node; node = node.parentElement) {
      node.scrollTop = 0;
    }
  };

  return (
    <div className="settings-rail-layout" ref={rootRef}>
      <nav className="settings-rail" aria-label="Settings sections" ref={railRef}>
        {sections.map((section) => (
          <button
            key={section.id}
            className="settings-rail-item"
            type="button"
            data-settings-section={section.id}
            aria-current={section === selected ? "true" : undefined}
            onClick={() => choose(section.id)}
          >
            {section.icon}
            <span className="settings-rail-label">{section.label}</span>
          </button>
        ))}
      </nav>
      <div className="settings-rail-panes">
        {sections.map((section) => (
          <div
            key={section.id}
            className="settings-pane"
            data-settings-pane={section.id}
            hidden={section !== selected}
          >
            {section.content}
          </div>
        ))}
        {children}
      </div>
    </div>
  );
}
