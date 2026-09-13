import { useCallback, useEffect, useState } from "react";
import {
  APPEARANCE_STORAGE_KEY,
  LEGACY_THEME_STORAGE_KEY,
  readStoredAppearance,
  resolveColorMode,
  resolveTheme,
  type AppearanceChoice,
  type ColorModeChoice,
  type ThemeChoice,
} from "../theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function prefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.(DARK_QUERY).matches === true;
}

function readChoice(): AppearanceChoice {
  try {
    return readStoredAppearance(
      localStorage.getItem(APPEARANCE_STORAGE_KEY),
      localStorage.getItem(LEGACY_THEME_STORAGE_KEY),
    );
  } catch {
    // Appearance is a convenience; storage failures must not affect the project.
    return { theme: "classic", mode: "system" };
  }
}

/** One app owner paints and remembers the independent theme and color mode. */
export function useTheme() {
  const [choice, setChoice] = useState<AppearanceChoice>(readChoice);
  const [systemDark, setSystemDark] = useState(prefersDark);

  useEffect(() => {
    const media = window.matchMedia?.(DARK_QUERY);
    if (!media) return;
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const resolvedMode = resolveColorMode(choice.mode, systemDark);
  const palette = resolveTheme(choice.theme, resolvedMode);

  useEffect(() => {
    document.documentElement.dataset.theme = choice.theme;
    document.documentElement.dataset.colorMode = resolvedMode;
  }, [choice.theme, resolvedMode]);

  useEffect(() => {
    try {
      localStorage.setItem(APPEARANCE_STORAGE_KEY, JSON.stringify(choice));
    } catch {
      // A preference that cannot be remembered still applies for this session.
    }
  }, [choice]);

  const setTheme = useCallback((theme: ThemeChoice) => {
    setChoice((current) => ({ ...current, theme }));
  }, []);
  const setMode = useCallback((mode: ColorModeChoice) => {
    setChoice((current) => ({ ...current, mode }));
  }, []);

  return { theme: choice.theme, mode: choice.mode, resolvedMode, palette, setTheme, setMode };
}
