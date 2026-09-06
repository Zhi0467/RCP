import { useCallback, useEffect, useState } from "react";
import {
  normalizeThemeChoice,
  resolveTheme,
  THEME_STORAGE_KEY,
  type ResolvedTheme,
  type ThemeChoice,
} from "../theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function prefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.(DARK_QUERY).matches === true;
}

function readChoice(): ThemeChoice {
  try {
    return normalizeThemeChoice(localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    // Appearance is a convenience; storage failures must not affect the project.
    return "system";
  }
}

/** Own the painted theme: persist the choice and stamp the resolved value on the root.
 *
 * The attribute, not a media query, is what the stylesheet reads, so an explicit
 * Light or Dark choice wins over the OS in both directions.
 */
export function useTheme(): {
  choice: ThemeChoice;
  resolved: ResolvedTheme;
  setChoice: (choice: ThemeChoice) => void;
} {
  const [choice, setChoiceState] = useState<ThemeChoice>(readChoice);
  const [systemDark, setSystemDark] = useState(prefersDark);

  useEffect(() => {
    const media = window.matchMedia?.(DARK_QUERY);
    if (!media) return;
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const resolved = resolveTheme(choice, systemDark);

  useEffect(() => {
    document.documentElement.dataset.theme = resolved;
  }, [resolved]);

  const setChoice = useCallback((next: ThemeChoice) => {
    setChoiceState(next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // A theme that cannot be remembered still applies for this session.
    }
  }, []);

  return { choice, resolved, setChoice };
}
