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

const STYLESHEET = readFileSync(
  fileURLToPath(new URL("../src/styles.css", import.meta.url)),
  "utf-8",
);

function rootTokens(selector) {
  const start = STYLESHEET.indexOf(`${selector} {`);
  assert.ok(start >= 0, `stylesheet must define ${selector}`);
  const block = STYLESHEET.slice(start, STYLESHEET.indexOf("}", start));
  return Object.fromEntries(
    [...block.matchAll(/(--[a-z0-9-]+):\s*([^;]+);/g)].map(([, name, value]) => [
      name,
      value.trim(),
    ]),
  );
}

/** Resolve one token for a theme, following var() indirection through the base palette. */
function resolveToken(name, theme) {
  const base = rootTokens(":root");
  const dark = rootTokens(':root[data-theme="dark"]');
  const lookup = theme === "dark" ? { ...base, ...dark } : base;
  let value = lookup[name];
  for (let hop = 0; hop < 4 && value?.startsWith("var("); hop += 1) {
    value = lookup[value.slice(4, value.indexOf(")")).trim()];
  }
  assert.match(value ?? "", /^#[0-9a-f]{6}$/i, `${name} must resolve to a hex color in ${theme}`);
  return value;
}

function luminance(hex) {
  const channels = hex
    .slice(1)
    .match(/.{2}/g)
    .map((value) => Number.parseInt(value, 16) / 255)
    .map((value) => (value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4)));
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

function contrastRatio(foreground, background) {
  const a = luminance(foreground);
  const b = luminance(background);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

test("text on the inverting ink surface stays readable in both themes", () => {
  for (const theme of ["light", "dark"]) {
    const ratio = contrastRatio(resolveToken("--paper", theme), resolveToken("--ink", theme));
    assert.ok(ratio >= 4.5, `${theme} ink surface contrast is ${ratio.toFixed(2)}:1`);
  }
});

test("no rule paints a literal text color on a theme-inverting background", () => {
  // Toasts and Attention badges once hardcoded white on --ink. That reads at
  // about 1.3:1 once --ink inverts, and a palette-pair sweep cannot see it
  // because the offending color is not a token.
  const offenders = [];
  for (const [, selector, body] of STYLESHEET.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const background = body.match(/background(?:-color)?:\s*([^;]+);/);
    const color = body.match(/(?<!-)color:\s*([^;]+);/);
    if (!background || !color) continue;
    const inverting = /var\(--(ink|walnut)\)/.test(background[1]);
    const literal = !/^(var\(|color-mix\(|inherit|currentColor|transparent)/.test(color[1].trim());
    if (inverting && literal) offenders.push(selector.trim().split("\n").pop().trim());
  }
  assert.deepEqual(offenders, []);
});
