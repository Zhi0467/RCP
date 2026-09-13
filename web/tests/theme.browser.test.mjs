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

async function openFixture(t, viewport = { width: 1100, height: 900 }) {
  const context = await browser.newContext({
    viewport,
    colorScheme: "light",
    reducedMotion: "reduce",
  });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("requestfailed", (request) => errors.push(request.url()));
  t.after(() => assert.deepEqual(errors, []));
  // Every API request stays inside this fixture, even if Settings gains a new read.
  await page.route("**/api/**", (route) => route.fulfill({ json: [] }));
  await page.goto(`${origin}/tests/fixtures/settingsRequestRaces.html`);
  await page.getByRole("group", { name: "Appearance", exact: true }).waitFor();
  return page;
}

async function assertTheme(page, choice, resolved) {
  await page.waitForFunction(
    ({ choice, resolved }) =>
      document.documentElement.dataset.theme === resolved &&
      localStorage.getItem("rcp:theme") === choice,
    { choice, resolved },
  );
  const labels = { system: "System", light: "Light", dark: "Dark", aqua: "Soft Aqua" };
  const appearance = page.getByRole("group", { name: "Appearance", exact: true });
  assert.equal(
    await appearance
      .getByRole("button", { name: labels[choice], exact: true })
      .getAttribute("aria-pressed"),
    "true",
  );
  assert.equal(await appearance.locator('[aria-pressed="true"]').count(), 1);
}

test("Settings remembers Soft Aqua across reload and OS changes, and keeps existing appearance modes", async (t) => {
  const page = await openFixture(t);
  const appearance = page.getByRole("group", { name: "Appearance", exact: true });
  await appearance.getByRole("button", { name: "Soft Aqua", exact: true }).click();
  await assertTheme(page, "aqua", "aqua");
  assert.equal(
    await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme),
    "light",
  );

  await page.emulateMedia({ colorScheme: "dark" });
  await assertTheme(page, "aqua", "aqua");
  await page.reload();
  await assertTheme(page, "aqua", "aqua");

  await appearance.getByRole("button", { name: "Light", exact: true }).click();
  await assertTheme(page, "light", "light");
  await page.emulateMedia({ colorScheme: "light" });
  await appearance.getByRole("button", { name: "Dark", exact: true }).click();
  await assertTheme(page, "dark", "dark");
  await appearance.getByRole("button", { name: "System", exact: true }).click();
  await assertTheme(page, "system", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await assertTheme(page, "system", "dark");
});

test("Soft Aqua appearance choices fit narrow screens and retain visible keyboard focus", async (t) => {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 360, height: 780 },
  ]) {
    const page = await openFixture(t, viewport);
    const appearance = page.getByRole("group", { name: "Appearance", exact: true });
    for (const label of ["System", "Light", "Dark", "Soft Aqua"]) {
      await appearance.getByRole("button", { name: label, exact: true }).click();
      const bounds = await appearance.locator("button").evaluateAll((buttons) =>
        buttons.map((button) => {
          const { left, right, width } = button.getBoundingClientRect();
          return {
            label: button.textContent,
            left,
            right,
            width,
            scrollWidth: button.scrollWidth,
            clientWidth: button.clientWidth,
          };
        }),
      );
      for (const button of bounds) {
        assert.ok(
          button.left >= 0 && button.right <= viewport.width,
          `${button.label} in ${label} must fit the ${viewport.width}px viewport: ${JSON.stringify(button)}`,
        );
        assert.ok(
          button.width > 0 && button.scrollWidth <= button.clientWidth,
          `${button.label} in ${label} must show its full label`,
        );
      }
    }
    await assertTheme(page, "aqua", "aqua");
    await appearance.getByRole("button", { name: "Dark", exact: true }).focus();
    await page.keyboard.press("Tab");
    const focused = await appearance
      .getByRole("button", { name: "Soft Aqua", exact: true })
      .evaluate((button) => {
        const style = getComputedStyle(button);
        return {
          keyboardFocus: button.matches(":focus-visible"),
          outlineStyle: style.outlineStyle,
          outlineWidth: Number.parseFloat(style.outlineWidth),
          outlineColor: style.outlineColor,
        };
      });
    assert.equal(focused.keyboardFocus, true);
    assert.notEqual(focused.outlineStyle, "none");
    assert.ok(focused.outlineWidth > 0);
    assert.notEqual(focused.outlineColor, "rgba(0, 0, 0, 0)");
  }
});

test("Soft Aqua gives real Settings buttons relief and fields an inset surface", async (t) => {
  const page = await openFixture(t);
  await page.getByRole("button", { name: "Soft Aqua", exact: true }).click();
  await assertTheme(page, "aqua", "aqua");
  const raised = page.getByRole("button", { name: "Resolve", exact: true });
  const recessed = page.getByLabel("Codex executable on local");
  const relief = await raised.evaluate((button) => getComputedStyle(button).boxShadow);
  const inset = await recessed.evaluate((field) => getComputedStyle(field).boxShadow);
  assert.notEqual(relief, "none", "An available action has raised relief");
  assert.match(inset, /inset/, "A text field has an inner shadow");
  assert.notEqual(relief, inset, "Actions and writing surfaces have distinct depth");

  await page.getByRole("button", { name: "Light", exact: true }).click();
  await assertTheme(page, "light", "light");
  await page.waitForFunction(
    ({ button, field, relief, inset }) =>
      getComputedStyle(button).boxShadow !== relief && getComputedStyle(field).boxShadow !== inset,
    { button: await raised.elementHandle(), field: await recessed.elementHandle(), relief, inset },
  );
});
