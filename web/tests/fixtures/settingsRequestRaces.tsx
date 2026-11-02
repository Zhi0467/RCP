import { activateProject, ResourceRuntime } from "./resourceRuntime";
import { projectFor } from "./settingsProject";
import { useLayoutEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { useTheme } from "../../src/ui/useTheme";
import { ProjectSettings } from "../../src/projects/ProjectSettings";
import type { IdentityResponse } from "../../src/core/types";
import "../../src/styles.css";

const publications: unknown[] = [];
Object.assign(window, { settingsProject: projectFor, settingsPublications: publications });
const teamIdentity: IdentityResponse | null = new URLSearchParams(window.location.search).has(
  "team",
)
  ? {
      space_id: "team",
      space_kind: "team",
      space_name: "Research team",
      user: {
        user_id: "self",
        display_name: "Researcher",
        identity_kind: "team_member",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        removal_started_at: null,
        removed_at: null,
      },
    }
  : null;

function Fixture() {
  useTheme();
  const [project, setProject] = useState(() => projectFor("alpha"));
  const [visible, setVisible] = useState(true);
  useLayoutEffect(() => activateProject(project.id), [project.id]);
  return (
    <main>
      <header>
        <button onClick={() => setProject(projectFor("alpha"))}>Open alpha</button>
        <button onClick={() => setProject(projectFor("beta"))}>Open beta</button>
        <button onClick={() => setVisible((current) => !current)}>Toggle settings</button>
        <output aria-label="Current project">{project.id}</output>
      </header>
      {visible && (
        <ProjectSettings
          apiBase={`/api/projects/${project.id}`}
          project={project}
          identity={teamIdentity}
          onLeftProject={() => undefined}
          usage={null}
          onRefreshUsage={async () => undefined}
          cacheClearDisabled={false}
          onSaved={(saved) => {
            publications.push({ kind: "saved", projectId: saved.id });
            setProject((current) => (current.id === saved.id ? saved : current));
          }}
          onCacheMetricsChange={(metrics) => publications.push({ kind: "cache", metrics })}
          onRefreshReadiness={async () => undefined}
          spaceKind={teamIdentity ? "team" : "personal"}
          consolidation={null}
          consolidationError={null}
          onConsolidationChanged={() => undefined}
        />
      )}
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <Fixture />
  </ResourceRuntime>,
);
