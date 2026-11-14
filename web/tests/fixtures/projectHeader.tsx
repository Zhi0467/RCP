import { createRoot } from "react-dom/client";
import { ArrowLeft, Files, FlaskConical, Inbox, LayoutList, Telescope } from "lucide-react";
import { useState } from "react";
import { GraphPicker } from "../../src/graph/GraphPicker";
import { ViewPicker } from "../../src/graph/ViewPicker";
import { PanelPicker } from "../../src/projects/PanelPicker";
import { ProjectDock } from "../../src/projects/ProjectDock";
import type { GraphRef } from "../../src/core/types";
import "../../src/styles.css";

const panels = [
  ["Overview", <LayoutList size={14} />],
  ["Inbox", <Inbox size={14} />],
  ["Runs", <FlaskConical size={14} />],
  ["Artifacts", <Files size={14} />],
] as const;
const tabs = [
  "Scaling laws for sparse mixtures",
  "Loop steer",
  "Paper draft",
  "Tokenizer ablations",
  "Reward model audit",
  "Long-context eval",
  "Data mixture search",
  "Distillation sweep",
].map((name, i) => ({ id: `p${i}`, name }));
const refs = [
  { kind: "main", name: "main", archived: false },
  {
    kind: "branch",
    branch_id: "fork",
    name: "fork-oct-19",
    archived: false,
    created_at: "2026-10-19",
    parent_head: { target: { kind: "main" }, revision: 1 },
  },
] as GraphRef[];

function Header() {
  const [panel, setPanel] = useState("Runs");
  const [active, setActive] = useState(tabs[tabs.length - 1].id);
  const current = panels.find(([label]) => label === panel)!;
  return (
    <header className="project-header">
      <div className="project-header-navigation">
        <button className="project-back" aria-label="All projects">
          <ArrowLeft size={16} />
        </button>
        <PanelPicker icon={current[1]} label={current[0]}>
          {panels.map(([label, icon]) => (
            <button
              key={label}
              className={label === panel ? "active" : ""}
              aria-current={label === panel ? "page" : undefined}
              onClick={() => setPanel(label)}
            >
              {icon}
              <span>{label}</span>
            </button>
          ))}
        </PanelPicker>
        <ProjectDock
          tabs={tabs}
          activeProjectId={active}
          onActivate={setActive}
          onClose={() => {}}
          onNew={() => {}}
        />
      </div>
      <div className="project-header-actions">
        <div className="project-header-group project-ref-group">
          <ViewPicker
            layout="scientific"
            trustView="working"
            onLayout={() => {}}
            onTrustView={() => {}}
          />
          <GraphPicker
            projectId="project"
            refs={refs}
            activeRef={{ kind: "branch", branch_id: "fork" }}
            onSelect={() => {}}
            onRefUpdated={() => {}}
          />
        </div>
        <div className="project-header-group project-action-group">
          <button className="button secondary">
            <Telescope size={14} /> <span className="auto-research-label">Auto-research</span>
          </button>
        </div>
      </div>
    </header>
  );
}

createRoot(document.getElementById("root")!).render(
  <div className="app-shell">
    <Header />
    <div style={{ position: "relative", zIndex: 15, height: 400, background: "gray" }} />
  </div>,
);
