export const THEME_STORAGE_KEY = "rcp:theme";

/** What the human asked for. "system" defers to the OS setting. */
export type ThemeChoice = "system" | "light" | "dark";

/** What actually gets painted, once "system" is resolved. */
export type ResolvedTheme = "light" | "dark";

export const THEME_CHOICES: ThemeChoice[] = ["system", "light", "dark"];

export function normalizeThemeChoice(value: unknown): ThemeChoice {
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

export function resolveTheme(choice: ThemeChoice, prefersDark: boolean): ResolvedTheme {
  if (choice === "system") return prefersDark ? "dark" : "light";
  return choice;
}

export function themeChoiceLabel(choice: ThemeChoice): string {
  return choice === "system" ? "System" : choice === "light" ? "Light" : "Dark";
}
