import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

import {
  APPEARANCE_STORAGE_KEY,
  THEME_CHOICES,
  colorModeChoiceLabel,
  themeChoiceLabel,
} from "../src/ui/theme.ts";
import { routeSpaceSettingsApi } from "./fixtures/spaceSettingsApi.mjs";

let server;
let browser;
let origin;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

async function openFixture(
  t,
  {
    fixture = "appearance",
    viewport = { width: 1100, height: 900 },
    legacy = null,
    colorScheme = "light",
  } = {},
) {
  const context = await browser.newContext({ viewport, colorScheme, reducedMotion: "reduce" });
  t.after(() => context.close());
  if (legacy)
    await context.addInitScript((value) => localStorage.setItem("rcp:theme", value), legacy);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("requestfailed", (request) => errors.push(request.url()));
  t.after(() => assert.deepEqual(errors, []));
  // Nothing in the real landing or Settings components can reach a human backend.
  await routeSpaceSettingsApi(page);
  await page.goto(`${origin}/tests/fixtures/${fixture}.html`);
  if (fixture === "appearance") await identityTrigger(page).waitFor();
  else await appearancePane(page).waitFor();
  return page;
}

function identityTrigger(page) {
  return page.locator(".landing-identity-trigger");
}

// The display preferences are Space Settings' Appearance pane.
function appearancePane(page) {
  return page.getByRole("region", { name: "Appearance", exact: true });
}

const choice = (group, label) => group.getByRole("button", { name: label, exact: true });
const themeButton = (panel, theme) =>
  choice(panel.getByRole("group", { name: "Theme", exact: true }), themeChoiceLabel(theme));
const modeButton = (panel, mode) =>
  choice(panel.getByRole("group", { name: "Color mode", exact: true }), colorModeChoiceLabel(mode));

async function assertAppearance(page, theme, mode, resolvedMode = mode) {
  await page.waitForFunction(
    ({ theme, mode, resolvedMode }) => {
      const stored = JSON.parse(localStorage.getItem("rcp:appearance") || "null");
      return (
        document.documentElement.dataset.theme === theme &&
        document.documentElement.dataset.colorMode === resolvedMode &&
        stored?.theme === theme &&
        stored?.mode === mode
      );
    },
    { theme, mode, resolvedMode },
  );
  assert.equal(
    await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme),
    resolvedMode,
  );
}

async function badgeColors(page) {
  const badge = page.locator(".space-runs .status-pill.actionable");
  await page.waitForFunction(
    (element) => {
      const style = getComputedStyle(element);
      return (
        style.backgroundColor === element.style.backgroundColor &&
        style.color === element.style.color
      );
    },
    await badge.elementHandle(),
  );
  return badge.evaluate((element) => {
    const style = getComputedStyle(element);
    return `${style.backgroundColor}/${style.color}`;
  });
}

test("Appearance keeps theme and mode independent, persists them, and follows the system mode", async (t) => {
  const page = await openFixture(t, { fixture: "appearanceSettings" });
  await assertAppearance(page, "aqua", "system", "light");
  const panel = appearancePane(page);
  const mode = panel.getByRole("group", { name: "Color mode", exact: true });
  const theme = panel.getByRole("group", { name: "Theme", exact: true });
  assert.ok((await mode.boundingBox()).y < (await theme.boundingBox()).y, "Mode precedes Theme");
  await themeButton(panel, "classic").click();
  await assertAppearance(page, "classic", "system", "light");
  await modeButton(panel, "dark").click();
  await assertAppearance(page, "classic", "dark");
  await themeButton(panel, "aqua").click();
  await assertAppearance(page, "aqua", "dark");
  await modeButton(panel, "light").click();
  await assertAppearance(page, "aqua", "light");

  await page.reload();
  await assertAppearance(page, "aqua", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await assertAppearance(page, "aqua", "light");
  await modeButton(panel, "system").click();
  await assertAppearance(page, "aqua", "system", "dark");
  await themeButton(panel, "classic").click();
  await assertAppearance(page, "classic", "system", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await assertAppearance(page, "classic", "system", "light");
  assert.equal(await modeButton(panel, "system").getAttribute("aria-pressed"), "true");
  assert.equal(await themeButton(panel, "classic").getAttribute("aria-pressed"), "true");
});

test("Runs badges take each theme and mode's palette from the stored appearance", async (t) => {
  const page = await openFixture(t);
  const colors = new Set();
  for (const theme of THEME_CHOICES) {
    for (const mode of ["light", "dark"]) {
      await page.evaluate(({ key, value }) => localStorage.setItem(key, JSON.stringify(value)), {
        key: APPEARANCE_STORAGE_KEY,
        value: { theme, mode },
      });
      await page.reload();
      await assertAppearance(page, theme, mode);
      colors.add(await badgeColors(page));
    }
  }
  assert.equal(
    colors.size,
    THEME_CHOICES.length * 2,
    `Every palette reaches the Runs rows: ${[...colors].join(", ")}`,
  );
});

test("legacy appearance migrates on mount and the new independent preference wins on reload", async (t) => {
  for (const legacy of ["system", "light", "dark", "aqua"]) {
    const page = await openFixture(t, {
      fixture: "appearanceSettings",
      legacy,
      colorScheme: "dark",
    });
    const mode = legacy === "aqua" ? "light" : legacy;
    await assertAppearance(page, "aqua", mode, mode === "system" ? "dark" : mode);
    const panel = appearancePane(page);
    await themeButton(panel, "classic").click();
    await modeButton(panel, "dark").click();
    await assertAppearance(page, "classic", "dark");
    await page.reload();
    await assertAppearance(page, "classic", "dark");
  }
});

test("Appearance fits narrow screens and exposes keyboard focus", async (t) => {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 360, height: 780 },
  ]) {
    const page = await openFixture(t, { fixture: "appearanceSettings", viewport });
    const panel = appearancePane(page);
    for (const material of THEME_CHOICES) {
      await themeButton(panel, material).click();
      for (const colorMode of ["system", "light", "dark"]) {
        await modeButton(panel, colorMode).click();
        const bounds = await panel.boundingBox();
        assert.ok(
          bounds.x >= 0 && bounds.x + bounds.width <= viewport.width,
          `${material} ${colorMode} panel fits ${viewport.width}px: ${JSON.stringify(bounds)}`,
        );
        const labelsFit = await panel
          .locator("button")
          .evaluateAll((buttons) =>
            buttons.every((button) => button.scrollWidth <= button.clientWidth),
          );
        assert.equal(labelsFit, true, "Every appearance label is fully visible");
      }
    }
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
      true,
      "The page does not scroll sideways",
    );
    await themeButton(panel, THEME_CHOICES[0]).focus();
    await page.keyboard.press("Tab");
    const focus = await themeButton(panel, THEME_CHOICES[1]).evaluate((button) => {
      const style = getComputedStyle(button);
      return {
        visible: button.matches(":focus-visible"),
        style: style.outlineStyle,
        width: Number.parseFloat(style.outlineWidth),
      };
    });
    assert.equal(focus.visible, true);
    assert.notEqual(focus.style, "none");
    assert.ok(focus.width > 0);
  }
});

test("the identity menu fits narrow screens, holds no picker, and dismisses accessibly", async (t) => {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 360, height: 780 },
  ]) {
    const page = await openFixture(t, { viewport });
    const trigger = identityTrigger(page);
    await trigger.focus();
    await page.keyboard.press("Enter");
    const panel = page.locator(".landing-identity-panel");
    await panel.waitFor();
    const bounds = await panel.boundingBox();
    assert.ok(
      bounds.x >= 0 && bounds.x + bounds.width <= viewport.width,
      `Identity menu fits ${viewport.width}px: ${JSON.stringify(bounds)}`,
    );
    assert.equal(await panel.locator(".appearance-picker").count(), 0);
    await page.keyboard.press("Tab");
    const focus = await page.evaluate(() => {
      const active = document.activeElement;
      return {
        inside: Boolean(active?.closest(".landing-identity-panel")),
        visible: active?.matches(":focus-visible") ?? false,
        style: active ? getComputedStyle(active).outlineStyle : "none",
      };
    });
    assert.deepEqual(
      { inside: focus.inside, visible: focus.visible },
      { inside: true, visible: true },
    );
    assert.notEqual(focus.style, "none");
    await page.keyboard.press("Escape");
    await panel.waitFor({ state: "hidden" });
    assert.equal(await trigger.evaluate((button) => document.activeElement === button), true);
    await trigger.click();
    await panel.waitFor();
    await page.locator(".landing-main").click({ position: { x: 4, y: 4 } });
    await panel.waitFor({ state: "hidden" });
  }
});

test("project Settings keeps the global appearance and no longer owns a Display picker or empty section", async (t) => {
  const page = await openFixture(t, { fixture: "appearanceSettings" });
  const panel = appearancePane(page);
  await themeButton(panel, "aqua").click();
  await modeButton(panel, "dark").click();
  await assertAppearance(page, "aqua", "dark");
  await page.goto(`${origin}/tests/fixtures/settingsRequestRaces.html`);
  const recessed = page.locator(".agent-auto-research-default input");
  await recessed.waitFor();
  await assertAppearance(page, "aqua", "dark");
  assert.equal(await page.locator(".display-settings").count(), 0);
  assert.equal(await page.getByRole("group", { name: "Theme", exact: true }).count(), 0);
  const raised = page.getByRole("button", { name: "Probe", exact: true });
  const relief = await raised.evaluate((button) => getComputedStyle(button).boxShadow);
  const inset = await recessed.evaluate((field) => getComputedStyle(field).boxShadow);
  assert.notEqual(relief, "none");
  assert.match(inset, /inset/);
  assert.notEqual(relief, inset);
});

test("an open project card menu stays above the shelf row below while its items are hovered", async (t) => {
  const page = await openFixture(t);
  const card = page.locator(".project-cover-shell").first();
  await card.hover();
  await card.getByRole("button", { name: "Project actions for Project One" }).click();
  const menu = card.getByRole("menu");
  const remove = menu.getByRole("menuitem", { name: "Delete project" });
  await remove.hover({ force: true });
  const covered = await menu.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const misses = [];
    for (let y = box.top + 2; y < box.bottom - 1; y += 6) {
      const hit = document.elementFromPoint(box.left + box.width / 2, y);
      if (!element.contains(hit)) misses.push(hit?.className);
    }
    return misses;
  });
  assert.deepEqual(covered, []);
});
