export const THEME_STORAGE_KEY = "rcp:theme";

/** What the human asked for. "system" defers to the OS setting. */
export type ThemeChoice = "system" | "light" | "dark" | "aqua";

/** What actually gets painted, once "system" is resolved. */
export type ResolvedTheme = "light" | "dark" | "aqua";

export const THEME_CHOICES: ThemeChoice[] = ["system", "light", "dark", "aqua"];

export function normalizeThemeChoice(value: unknown): ThemeChoice {
  return value === "light" || value === "dark" || value === "aqua" || value === "system"
    ? value
    : "system";
}

export function resolveTheme(choice: ThemeChoice, prefersDark: boolean): ResolvedTheme {
  if (choice === "system") return prefersDark ? "dark" : "light";
  return choice;
}

export function themeChoiceLabel(choice: ThemeChoice): string {
  switch (choice) {
    case "system":
      return "System";
    case "light":
      return "Light";
    case "dark":
      return "Dark";
    case "aqua":
      return "Soft Aqua";
  }
}
