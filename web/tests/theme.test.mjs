import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";

import {
  normalizeThemeChoice,
  resolveTheme,
  THEME_CHOICES,
  themeChoiceLabel,
} from "../src/theme.ts";

test("an unreadable stored choice falls back to following the system", () => {
  assert.equal(normalizeThemeChoice(null), "system");
  assert.equal(normalizeThemeChoice(""), "system");
  assert.equal(normalizeThemeChoice("sepia"), "system");
  assert.equal(normalizeThemeChoice(undefined), "system");
  assert.equal(normalizeThemeChoice("dark"), "dark");
  assert.equal(normalizeThemeChoice("light"), "light");
});

test("an explicit choice outranks the system preference in both directions", () => {
  assert.equal(resolveTheme("light", true), "light");
  assert.equal(resolveTheme("dark", false), "dark");
  assert.equal(resolveTheme("system", true), "dark");
  assert.equal(resolveTheme("system", false), "light");
});

test("every offered choice has a label", () => {
  assert.deepEqual(THEME_CHOICES, ["system", "light", "dark"]);
  for (const choice of THEME_CHOICES) {
    assert.ok(themeChoiceLabel(choice).length > 0);
  }
});

const PRE_PAINT_SCRIPT = (() => {
  const html = readFileSync(fileURLToPath(new URL("../index.html", import.meta.url)), "utf-8");
  const match = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(match, "index.html must carry the pre-paint theme script");
  return match[1];
})();

/** Run the shipped pre-paint script and report the theme it stamped. */
function stampedTheme({ stored, systemDark }) {
  const documentElement = { dataset: {} };
  runInNewContext(PRE_PAINT_SCRIPT, {
    document: { documentElement },
    localStorage: {
      getItem() {
        if (stored instanceof Error) throw stored;
        return stored;
      },
    },
    window: {
      matchMedia(query) {
        return { matches: query === "(prefers-color-scheme: dark)" && systemDark };
      },
    },
  });
  return documentElement.dataset.theme;
}

test("the pre-paint script stamps the same theme the hook would resolve", () => {
  assert.equal(stampedTheme({ stored: "dark", systemDark: false }), "dark");
  assert.equal(stampedTheme({ stored: "light", systemDark: true }), "light");
  assert.equal(stampedTheme({ stored: null, systemDark: true }), "dark");
  assert.equal(stampedTheme({ stored: "sepia", systemDark: true }), "dark");
  assert.equal(stampedTheme({ stored: null, systemDark: false }), "light");
});

test("blocked storage still honours a dark system preference before first paint", () => {
  // Storage can throw outright when a privacy policy blocks it. Falling back to
  // light there would produce the exact flash this script exists to prevent.
  const blocked = new Error("storage is not available");
  assert.equal(stampedTheme({ stored: blocked, systemDark: true }), "dark");
  assert.equal(stampedTheme({ stored: blocked, systemDark: false }), "light");
});
