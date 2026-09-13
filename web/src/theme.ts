export const APPEARANCE_STORAGE_KEY = "rcp:appearance";
export const LEGACY_THEME_STORAGE_KEY = "rcp:theme";

export type ThemeChoice = "classic" | "aqua";
export type ColorModeChoice = "system" | "light" | "dark";
export type ResolvedColorMode = "light" | "dark";
export type ResolvedTheme = `${ThemeChoice}-${ResolvedColorMode}`;
export interface AppearanceChoice {
  theme: ThemeChoice;
  mode: ColorModeChoice;
}

export const THEME_CHOICES: ThemeChoice[] = ["classic", "aqua"];
export const COLOR_MODE_CHOICES: ColorModeChoice[] = ["system", "light", "dark"];

export function normalizeThemeChoice(value: unknown): ThemeChoice {
  return value === "aqua" ? "aqua" : "classic";
}

export function normalizeColorModeChoice(value: unknown): ColorModeChoice {
  return value === "light" || value === "dark" ? value : "system";
}

/** Preserve the old painted appearance while separating its two choices. */
export function readStoredAppearance(
  stored: string | null,
  legacy: string | null,
): AppearanceChoice {
  try {
    const parsed: unknown = JSON.parse(stored ?? "null");
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const record = parsed as Record<string, unknown>;
      return {
        theme: normalizeThemeChoice(record.theme),
        mode: normalizeColorModeChoice(record.mode),
      };
    }
  } catch {
    // An unreadable new preference can still recover the previous choice.
  }
  return legacy === "aqua"
    ? { theme: "aqua", mode: "light" }
    : { theme: "classic", mode: normalizeColorModeChoice(legacy) };
}

export function resolveColorMode(choice: ColorModeChoice, prefersDark: boolean): ResolvedColorMode {
  return choice === "system" ? (prefersDark ? "dark" : "light") : choice;
}

export function resolveTheme(theme: ThemeChoice, mode: ResolvedColorMode): ResolvedTheme {
  return `${theme}-${mode}`;
}

export function themeChoiceLabel(choice: ThemeChoice): string {
  return choice === "aqua" ? "Aqua" : "Classic";
}

export function colorModeChoiceLabel(choice: ColorModeChoice): string {
  return choice === "system" ? "System" : choice === "light" ? "Light" : "Dark";
}
