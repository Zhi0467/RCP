import { createRoot } from "react-dom/client";
import { ResourceRuntime } from "./resourceRuntime";
import { useTheme } from "../../src/ui/useTheme";
import { SpaceSettings } from "../../src/projects/SpaceSettings";
import "../../src/styles.css";

const noop = () => undefined;

// Space Settings opens on Appearance, where the display preferences live.
function Fixture() {
  const appearance = useTheme();
  return (
    <SpaceSettings
      spaceKind="personal"
      initialSection="appearance"
      appearance={{
        themeChoice: appearance.theme,
        colorModeChoice: appearance.mode,
        onThemeChoiceChange: appearance.setTheme,
        onColorModeChoiceChange: appearance.setMode,
      }}
      cacheProjectId={null}
      cacheClearDisabled={false}
      onAllCachesCleared={noop}
      onClose={noop}
    />
  );
}

createRoot(document.getElementById("root")!).render(
  <ResourceRuntime>
    <Fixture />
  </ResourceRuntime>,
);
