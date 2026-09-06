import assert from "node:assert/strict";
import test from "node:test";

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
