import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

import { MACHINE_POWER_UNSUPPORTED, routeSpaceSettingsApi } from "./fixtures/spaceSettingsApi.mjs";

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

async function openRail(
  t,
  { query = "", viewport = { width: 1280, height: 900 }, machinePower, configure } = {},
) {
  const context = await browser.newContext({ viewport, reducedMotion: "reduce" });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  t.after(() => assert.deepEqual(errors, []));
  await routeSpaceSettingsApi(page, { machinePower });
  await configure?.(page);
  await page.goto(`${origin}/tests/fixtures/settingsRail.html${query}`);
  await page.locator(".settings-rail").waitFor();
  return page;
}

const railItem = (page, id) => page.locator(`.settings-rail [data-settings-section="${id}"]`);
const select = (page, id) => railItem(page, id).click();
const railIds = (page) =>
  page
    .locator(".settings-rail [data-settings-section]")
    .evaluateAll((items) => items.map((item) => item.dataset.settingsSection));
const current = (page) =>
  page
    .locator('.settings-rail [aria-current="true"]')
    .evaluateAll((items) => items.map((item) => item.dataset.settingsSection));
const shownPanes = (page) =>
  page
    .locator(".settings-pane")
    .evaluateAll((panes) =>
      panes.filter((pane) => pane.checkVisibility()).map((pane) => pane.dataset.settingsPane),
    );

test("choosing a rail item shows only its pane, and a draft in another pane survives", async (t) => {
  let machineReads = 0;
  const page = await openRail(t, {
    configure: (page) =>
      page.route("**/api/space/machines", (route) => {
        machineReads += 1;
        return route.fulfill({ json: { machines: [] } });
      }),
  });
  assert.deepEqual(await shownPanes(page), ["appearance"]);

  await select(page, "machines");
  assert.deepEqual(await shownPanes(page), ["machines"]);
  assert.deepEqual(await current(page), ["machines"]);
  await page.locator(".space-machine-add").click();
  const name = page.locator("[data-new-machine-form] input").first();
  await name.fill("Draft box");

  await select(page, "voice");
  assert.deepEqual(await shownPanes(page), ["voice"]);
  assert.equal(await name.isVisible(), false);
  await select(page, "machines");
  assert.equal(await name.inputValue(), "Draft box");
  assert.equal(machineReads, 1, "Switching panes never refetches");
});

test("the initial section selects its pane, and an unknown one opens the first", async (t) => {
  const voice = await openRail(t, { query: "?section=voice" });
  assert.deepEqual(await shownPanes(voice), ["voice"]);
  assert.deepEqual(await current(voice), ["voice"]);

  const unknown = await openRail(t, { query: "?section=nowhere" });
  const [first] = await railIds(unknown);
  assert.deepEqual(await shownPanes(unknown), [first]);
});

test("on a phone the rail strip keeps its selected item in view without widening the page", async (t) => {
  for (const width of [390, 360]) {
    const page = await openRail(t, {
      query: "?section=caches",
      viewport: { width, height: 800 },
    });
    assert.deepEqual(await shownPanes(page), ["caches"]);
    const fits = await page.evaluate(() => {
      const rail = document.querySelector(".settings-rail").getBoundingClientRect();
      const item = document.querySelector('.settings-rail [aria-current="true"]');
      const box = item.getBoundingClientRect();
      return {
        item: box.left >= rail.left - 1 && box.right <= rail.right + 1,
        page: document.documentElement.scrollWidth <= window.innerWidth,
      };
    });
    assert.deepEqual(fits, { item: true, page: true }, `${width}px`);
  }
});

test("opening again remounts Space Settings on the requested section", async (t) => {
  const page = await openRail(t);
  await select(page, "voice");
  await page.evaluate(() => window.openSpaceSettings("machines"));
  await page.waitForFunction(
    () => document.querySelector('[data-settings-pane="machines"]')?.checkVisibility() === true,
  );
  assert.deepEqual(await shownPanes(page), ["machines"]);
  await select(page, "voice");
  await page.evaluate(() => window.openSpaceSettings("machines"));
  await page.waitForFunction(
    () => document.querySelector('[data-settings-pane="machines"]')?.checkVisibility() === true,
  );
  assert.deepEqual(await current(page), ["machines"]);
});

test("This Mac waits in the rail while its status loads and leaves only when unsupported", async (t) => {
  for (const supported of [true, false]) {
    const held = Promise.withResolvers();
    const release = Promise.withResolvers();
    const page = await openRail(t, {
      query: "?section=this-mac",
      machinePower: null,
      configure: (page) =>
        page.route("**/api/machine-power", async (route) => {
          held.resolve();
          await release.promise;
          await route.fulfill({ json: { ...MACHINE_POWER_UNSUPPORTED, supported } });
        }),
    });
    await held.promise;
    assert.deepEqual(await shownPanes(page), ["this-mac"]);
    assert.deepEqual(await current(page), ["this-mac"]);

    release.resolve();
    if (supported) {
      await page.locator(".machine-power-settings").waitFor();
      assert.deepEqual(await shownPanes(page), ["this-mac"]);
    } else {
      await railItem(page, "this-mac").waitFor({ state: "detached" });
      const [first] = await railIds(page);
      assert.deepEqual(await shownPanes(page), [first]);
    }
  }
});

const ceiling = (page) => page.locator(".agent-auto-research-default input");

test("switching Project Settings to another project starts on its first section", async (t) => {
  const page = await openRail(t, { query: "?project" });
  const [first] = await railIds(page);
  await select(page, "agent-defaults");
  assert.deepEqual(await shownPanes(page), ["agent-defaults"]);
  await page.getByRole("button", { name: "Open beta", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector(".agent-auto-research-default input")?.value === "20",
  );
  assert.deepEqual(await shownPanes(page), [first]);
  assert.deepEqual(await current(page), [first]);
});

test("the save bar shows only on panes with staged fields until there are edits", async (t) => {
  const page = await openRail(t, { query: "?project" });
  const bar = page.locator(".settings-savebar");
  for (const [pane, shown] of [
    ["members", false],
    ["agent-defaults", true],
    ["skills", true],
    ["repos", false],
  ]) {
    await select(page, pane);
    assert.equal(await bar.count(), shown ? 1 : 0, pane);
  }
  await select(page, "agent-defaults");
  await ceiling(page).fill("12");
  await select(page, "members");
  assert.equal(await bar.count(), 1);
});

test("an invalid ceiling shows in the save bar on every pane and keeps Save disabled", async (t) => {
  const page = await openRail(t, { query: "?project" });
  const status = page.locator(".settings-savebar .settings-save-status");
  const save = page.locator(".settings-savebar .button.primary");
  await select(page, "agent-defaults");
  await ceiling(page).fill("0");
  for (const pane of ["repos", "agent-defaults"]) {
    await select(page, pane);
    assert.deepEqual(await shownPanes(page), [pane]);
    assert.equal(await status.isVisible(), true);
    assert.match(await status.getAttribute("class"), /\berror\b/);
    assert.notEqual((await status.innerText()).trim(), "");
    assert.equal(await save.isDisabled(), true);
  }
  await ceiling(page).fill("5");
  await select(page, "repos");
  assert.doesNotMatch(await status.getAttribute("class"), /\berror\b/);
  assert.equal(await save.isDisabled(), false);
});

test("Seed and Refresh share one row, and a save gives Refresh the Seed profile", async (t) => {
  let sent;
  const saved = new Promise((resolve) => (sent = resolve));
  const page = await openRail(t, {
    query: "?project",
    // The save is held open: only the request body matters here.
    configure: (page) =>
      page.route("**/api/projects/alpha/settings", (route) =>
        route.request().method() === "PUT"
          ? sent(route.request().postDataJSON())
          : route.fallback(),
      ),
  });
  await select(page, "agent-defaults");
  assert.equal(await page.locator(".settings-agent").count(), 5);
  await ceiling(page).fill("12");
  await page.locator(".settings-savebar .button.primary").click();
  const body = await saved;
  assert.deepEqual(body.agent_profiles.refresh, body.agent_profiles.seed);
  assert.equal(Object.keys(body.agent_profiles).length, 6);
});
