import { createRoot } from "react-dom/client";
import {
  ChevronUp,
  Files,
  FlaskConical,
  GitBranch,
  Inbox,
  LayoutList,
  MessageCircle,
  Settings,
  SquareTerminal,
} from "lucide-react";
import { GraphPicker } from "../../src/graph/GraphPicker";
import { ViewPicker } from "../../src/graph/ViewPicker";
import type { GraphRef } from "../../src/core/types";
import "../../src/styles.css";

// The Research tab's strip as App renders it, with a fork selected.
const tabs = [
  ["Overview", <LayoutList size={14} />],
  ["Inbox", <Inbox size={14} />],
  ["Research", <GitBranch size={14} />],
  ["Runs", <FlaskConical size={14} />],
  ["Artifacts", <Files size={14} />],
  ["Terminals", <SquareTerminal size={14} />],
  ["Agents", <MessageCircle size={14} />],
  ["Settings", <Settings size={14} />],
] as const;
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

createRoot(document.getElementById("root")!).render(
  <div className="app-shell">
    <nav className="project-tabs" aria-label="Project panels">
      <button className="project-tabs-toggle" aria-label="Collapse project header">
        <ChevronUp size={16} />
      </button>
      {tabs.map(([label, icon]) => (
        <button key={label} className={label === "Research" ? "active" : ""}>
          {icon}
          <span>{label}</span>
        </button>
      ))}
      <div className="project-tabs-trailing">
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
    </nav>
  </div>,
);
