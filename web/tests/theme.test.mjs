import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { runInNewContext } from "node:vm";
import {
  APPEARANCE_STORAGE_KEY,
  LEGACY_THEME_STORAGE_KEY,
  COLOR_MODE_CHOICES,
  THEME_CHOICES,
  normalizeColorModeChoice,
  normalizeThemeChoice,
  readStoredAppearance,
  resolveColorMode,
  resolveTheme,
} from "../src/theme.ts";
import { appStylesheet } from "./appStylesheet.mjs";

test("theme and color mode normalize independently with Aqua and System defaults", () => {
  for (const value of [null, undefined, "", "sepia", "dark", "system"]) {
    assert.equal(normalizeThemeChoice(value), "aqua");
  }
  assert.equal(normalizeThemeChoice("aqua"), "aqua");
  assert.equal(normalizeThemeChoice("classic"), "classic");
  for (const value of [null, undefined, "", "aqua"]) {
    assert.equal(normalizeColorModeChoice(value), "system");
  }
  assert.equal(normalizeColorModeChoice("light"), "light");
  assert.equal(normalizeColorModeChoice("dark"), "dark");
});

test("legacy preferences adopt Aqua while preserving mode, and saved themes take precedence", () => {
  for (const legacy of ["light", "dark", "system"]) {
    assert.deepEqual(readStoredAppearance(null, legacy), { theme: "aqua", mode: legacy });
  }
  assert.deepEqual(readStoredAppearance(null, "aqua"), { theme: "aqua", mode: "light" });
  assert.deepEqual(readStoredAppearance(null, null), { theme: "aqua", mode: "system" });
  assert.deepEqual(readStoredAppearance("broken", "dark"), { theme: "aqua", mode: "dark" });
  assert.deepEqual(readStoredAppearance('{"theme":"classic","mode":"system"}', "aqua"), {
    theme: "classic",
    mode: "system",
  });
  assert.deepEqual(readStoredAppearance('{"theme":"aqua","mode":"dark"}', "light"), {
    theme: "aqua",
    mode: "dark",
  });
  assert.deepEqual(readStoredAppearance('{"theme":"aqua","mode":"invalid"}', "dark"), {
    theme: "aqua",
    mode: "system",
  });
});

test("every theme supports explicit modes and follows the OS only in System mode", () => {
  for (const theme of THEME_CHOICES) {
    for (const prefersDark of [true, false]) {
      assert.equal(resolveTheme(theme, resolveColorMode("light", prefersDark)), `${theme}-light`);
      assert.equal(resolveTheme(theme, resolveColorMode("dark", prefersDark)), `${theme}-dark`);
      assert.equal(
        resolveTheme(theme, resolveColorMode("system", prefersDark)),
        `${theme}-${prefersDark ? "dark" : "light"}`,
      );
    }
  }
});

const PRE_PAINT_SCRIPT = (() => {
  const html = readFileSync(fileURLToPath(new URL("../index.html", import.meta.url)), "utf-8");
  const match = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(match, "index.html must carry the pre-paint theme script");
  return match[1];
})();

function stampedAppearance({ stored = null, legacy = null, systemDark }) {
  const documentElement = { dataset: {} };
  runInNewContext(PRE_PAINT_SCRIPT, {
    document: { documentElement },
    localStorage: {
      getItem(key) {
        if (stored instanceof Error) throw stored;
        return key === APPEARANCE_STORAGE_KEY
          ? stored
          : key === LEGACY_THEME_STORAGE_KEY
            ? legacy
            : null;
      },
    },
    window: {
      matchMedia(query) {
        return { matches: query === "(prefers-color-scheme: dark)" && systemDark };
      },
    },
  });
  return { ...documentElement.dataset };
}

test("shipped pre-paint and runtime agree for both axes and legacy migration", () => {
  const preferences = [
    null,
    "broken",
    "null",
    "[]",
    "{}",
    ...THEME_CHOICES.flatMap((theme) =>
      COLOR_MODE_CHOICES.map((mode) => JSON.stringify({ theme, mode })),
    ),
  ];
  for (const stored of preferences) {
    for (const legacy of [null, "light", "dark", "system", "aqua", "sepia"]) {
      for (const systemDark of [false, true]) {
        const choice = readStoredAppearance(stored, legacy);
        assert.deepEqual(
          stampedAppearance({ stored, legacy, systemDark }),
          {
            theme: choice.theme,
            colorMode: resolveColorMode(choice.mode, systemDark),
          },
          JSON.stringify({ stored, legacy, systemDark }),
        );
      }
    }
  }
});

test("blocked storage still follows the OS before first paint", () => {
  for (const systemDark of [false, true]) {
    assert.deepEqual(stampedAppearance({ stored: new Error("blocked"), systemDark }), {
      theme: "aqua",
      colorMode: systemDark ? "dark" : "light",
    });
  }
});

const STYLESHEET = appStylesheet();

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
  const [material, mode] = theme.split("-");
  const lookup = { ...base };
  if (mode === "dark") Object.assign(lookup, rootTokens(':root[data-color-mode="dark"]'));
  if (material === "aqua") {
    Object.assign(lookup, rootTokens(':root[data-theme="aqua"]'));
    if (mode === "dark")
      Object.assign(lookup, rootTokens(':root[data-theme="aqua"][data-color-mode="dark"]'));
  }
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

test("text on the inverting ink surface stays readable in every theme and color mode", () => {
  for (const theme of ["classic-light", "classic-dark", "aqua-light", "aqua-dark"]) {
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
