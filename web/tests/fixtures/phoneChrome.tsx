import { useState } from "react";
import { createRoot } from "react-dom/client";
import { History, Inbox, LayoutList, Settings } from "lucide-react";
import { PhoneProjectBar, PhoneTabBar } from "../../src/projects/PhoneProjectChrome";
import { ProjectDock } from "../../src/projects/ProjectDock";
import { ViewPicker } from "../../src/graph/ViewPicker";
import "../../src/styles.css";

declare global {
  interface Window {
    chosen: string[];
  }
}
window.chosen = [];

function Fixture() {
  const [view, setView] = useState("overview");
  const tab = (name: string, label: string, icon: React.ReactNode) => (
    <button
      key={name}
      className={view === name ? "active" : ""}
      onClick={() => {
        setView(name);
        window.chosen.push(name);
      }}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
  return (
    <div className="app-shell overview-shell">
      <PhoneProjectBar
        projectName="A project name long enough to need truncating on a phone"
        hasDraft
        onBack={() => window.chosen.push("back")}
        dock={
          <ProjectDock
            tabs={Array.from({ length: 30 }, (_, index) => ({
              id: `project-${index}`,
              name: `Project ${index}`,
            }))}
            activeProjectId="project-0"
            onActivate={(id) => window.chosen.push(id)}
            onClose={() => {}}
          />
        }
        sync={
          <div className="header-sync-side">
            <button className="button draft-sync active">Sync</button>
          </div>
        }
        menu={
          <>
            <ViewPicker
              inline
              layout="scientific"
              trustView="working"
              onLayout={(layout) => window.chosen.push(layout)}
              onTrustView={(trustView) => window.chosen.push(trustView)}
            />
            <button
              className="icon-button"
              aria-label="Project history"
              onClick={() => window.chosen.push("history")}
            >
              <History size={16} />
            </button>
          </>
        }
      />
      <main className="project-panel">
        {Array.from({ length: 60 }, (_, index) => (
          <p key={index}>Row {index}</p>
        ))}
      </main>
      <PhoneTabBar
        primary={[
          tab("overview", "Overview", <LayoutList size={14} />),
          tab("attention", "Inbox", <Inbox size={14} />),
        ]}
        more={[tab("settings", "Settings", <Settings size={14} />)]}
        moreActive={view === "settings"}
      />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
