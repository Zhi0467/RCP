import { useLayoutEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { activateProject, ResourceRuntime } from "./resourceRuntime";
import { projectFor } from "./settingsProject";
import { useTheme } from "../../src/ui/useTheme";
import { SpaceSettings, type SpaceSettingsSectionId } from "../../src/projects/SpaceSettings";
import { ProjectSettings } from "../../src/projects/ProjectSettings";
import "../../src/styles.css";

const noop = () => undefined;
const resolved = async () => undefined;
const params = new URLSearchParams(location.search);

// Opens the way App does: every open counts up and remounts on its section.
function Space() {
  const appearance = useTheme();
  const [open, setOpen] = useState({
    section: (params.get("section") ?? undefined) as SpaceSettingsSectionId | undefined,
    opened: 0,
  });
  Object.assign(window, {
    openSpaceSettings: (section: SpaceSettingsSectionId) =>
      setOpen((current) => ({ section, opened: current.opened + 1 })),
  });
  return (
    <SpaceSettings
      key={open.opened}
      initialSection={open.section}
      spaceKind="personal"
      appearance={{
        themeChoice: appearance.theme,
        colorModeChoice: appearance.mode,
        onThemeChoiceChange: appearance.setTheme,
        onColorModeChoiceChange: appearance.setMode,
      }}
      cacheProjectId="project"
      cacheClearDisabled={false}
      onAllCachesCleared={noop}
      onClose={noop}
    />
  );
}

// Project Settings stays mounted while the shown project changes.
function Project() {
  useTheme();
  const [project, setProject] = useState(() => projectFor("alpha"));
  useLayoutEffect(() => activateProject(project.id), [project.id]);
  return (
    <main>
      <button onClick={() => setProject(projectFor("beta"))}>Open beta</button>
      <ProjectSettings
        apiBase={`/api/projects/${project.id}`}
        project={project}
        identity={null}
        onLeftProject={noop}
        usage={null}
        onRefreshUsage={resolved}
        cacheClearDisabled={false}
        onSaved={noop}
        onCacheMetricsChange={noop}
        onRefreshReadiness={resolved}
        spaceKind="personal"
        consolidation={null}
        consolidationError={null}
        onConsolidationChanged={noop}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>{params.has("project") ? <Project /> : <Space />}</ResourceRuntime>,
);
