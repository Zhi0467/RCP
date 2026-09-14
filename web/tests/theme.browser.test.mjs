import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

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
  { viewport = { width: 1100, height: 900 }, legacy = null, colorScheme = "light" } = {},
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
  await page.route("**/api/**", (route) => route.fulfill({ json: [] }));
  await page.goto(`${origin}/tests/fixtures/appearance.html`);
  await page.getByRole("button", { name: "Display", exact: true }).waitFor();
  return page;
}

async function openDisplay(page) {
  await page.getByRole("button", { name: "Display", exact: true }).click();
  return page.getByRole("region", { name: "Display", exact: true });
}

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

test("landing Display keeps theme and mode independent, persists them, and updates run badges immediately", async (t) => {
  const page = await openFixture(t);
  await assertAppearance(page, "aqua", "system", "light");
  const panel = await openDisplay(page);
  const mode = panel.getByRole("group", { name: "Color mode", exact: true });
  const theme = panel.getByRole("group", { name: "Theme", exact: true });
  assert.ok((await mode.boundingBox()).y < (await theme.boundingBox()).y, "Mode precedes Theme");
  await theme.getByRole("button", { name: "Classic", exact: true }).click();
  await assertAppearance(page, "classic", "system", "light");
  const colors = new Set([await badgeColors(page)]);

  await mode.getByRole("button", { name: "Dark", exact: true }).click();
  await assertAppearance(page, "classic", "dark");
  colors.add(await badgeColors(page));
  await theme.getByRole("button", { name: "Aqua", exact: true }).click();
  await assertAppearance(page, "aqua", "dark");
  colors.add(await badgeColors(page));
  await mode.getByRole("button", { name: "Light", exact: true }).click();
  await assertAppearance(page, "aqua", "light");
  colors.add(await badgeColors(page));
  assert.equal(
    colors.size,
    4,
    `All four palettes reach the already-mounted Runs rows: ${[...colors].join(", ")}`,
  );

  await page.reload();
  await assertAppearance(page, "aqua", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await assertAppearance(page, "aqua", "light");
  await openDisplay(page);
  await mode.getByRole("button", { name: "System", exact: true }).click();
  await assertAppearance(page, "aqua", "system", "dark");
  await theme.getByRole("button", { name: "Classic", exact: true }).click();
  await assertAppearance(page, "classic", "system", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await assertAppearance(page, "classic", "system", "light");
  assert.equal(
    await mode.getByRole("button", { name: "System", exact: true }).getAttribute("aria-pressed"),
    "true",
  );
  assert.equal(
    await theme.getByRole("button", { name: "Classic", exact: true }).getAttribute("aria-pressed"),
    "true",
  );
});

test("legacy appearance migrates on mount and the new independent preference wins on reload", async (t) => {
  for (const legacy of ["system", "light", "dark", "aqua"]) {
    const page = await openFixture(t, { legacy, colorScheme: "dark" });
    const mode = legacy === "aqua" ? "light" : legacy;
    await assertAppearance(page, "aqua", mode, mode === "system" ? "dark" : mode);
    const panel = await openDisplay(page);
    await panel
      .getByRole("group", { name: "Theme", exact: true })
      .getByRole("button", { name: "Classic", exact: true })
      .click();
    await panel
      .getByRole("group", { name: "Color mode", exact: true })
      .getByRole("button", { name: "Dark", exact: true })
      .click();
    await assertAppearance(page, "classic", "dark");
    await page.reload();
    await assertAppearance(page, "classic", "dark");
  }
});

test("landing Display fits narrow screens, exposes keyboard focus, and dismisses accessibly", async (t) => {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 360, height: 780 },
  ]) {
    const page = await openFixture(t, { viewport });
    const trigger = page.getByRole("button", { name: "Display", exact: true });
    await trigger.focus();
    await page.keyboard.press("Enter");
    const panel = page.getByRole("region", { name: "Display", exact: true });
    const mode = panel.getByRole("group", { name: "Color mode", exact: true });
    const theme = panel.getByRole("group", { name: "Theme", exact: true });
    for (const material of ["Classic", "Aqua"]) {
      await theme.getByRole("button", { name: material, exact: true }).click();
      for (const colorMode of ["System", "Light", "Dark"]) {
        await mode.getByRole("button", { name: colorMode, exact: true }).click();
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
    await theme.getByRole("button", { name: "Classic", exact: true }).focus();
    await page.keyboard.press("Tab");
    const focus = await theme
      .getByRole("button", { name: "Aqua", exact: true })
      .evaluate((button) => {
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
    await page.keyboard.press("Escape");
    await panel.waitFor({ state: "detached" });
    assert.equal(await trigger.evaluate((button) => document.activeElement === button), true);
    await trigger.click();
    await page.getByRole("button", { name: /Use existing checkout/ }).click();
    await panel.waitFor({ state: "detached" });
  }
});

test("project Settings keeps the global appearance and no longer owns a Display picker or empty section", async (t) => {
  const page = await openFixture(t);
  const panel = await openDisplay(page);
  await panel.getByRole("button", { name: "Aqua", exact: true }).click();
  await panel.getByRole("button", { name: "Dark", exact: true }).click();
  await assertAppearance(page, "aqua", "dark");
  await page.goto(`${origin}/tests/fixtures/settingsRequestRaces.html`);
  const recessed = page.getByLabel("Codex executable on local");
  await recessed.waitFor();
  await assertAppearance(page, "aqua", "dark");
  assert.equal(await page.locator(".display-settings").count(), 0);
  assert.equal(await page.getByRole("group", { name: "Theme", exact: true }).count(), 0);
  const raised = page.getByRole("button", { name: "Resolve", exact: true });
  const relief = await raised.evaluate((button) => getComputedStyle(button).boxShadow);
  const inset = await recessed.evaluate((field) => getComputedStyle(field).boxShadow);
  assert.notEqual(relief, "none");
  assert.match(inset, /inset/);
  assert.notEqual(relief, inset);
});
