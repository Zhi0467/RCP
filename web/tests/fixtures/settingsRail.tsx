import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ResourceRuntime } from "./resourceRuntime";
import { useTheme } from "../../src/ui/useTheme";
import { SpaceSettings, type SpaceSettingsSectionId } from "../../src/projects/SpaceSettings";
import "../../src/styles.css";

const noop = () => undefined;
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

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <Space />
  </ResourceRuntime>,
);
