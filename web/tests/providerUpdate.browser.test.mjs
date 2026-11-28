import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import test from "node:test";
import { createServer } from "vite";
import { chromium } from "playwright";
import { MACHINE, loginRow, settingsFor } from "./fixtures/signInRows.mjs";

const shots = process.env.RCP_LIMIT_SHOTS;
const RESETS_AT = "2026-11-27T16:10:00Z";

const ready = (provider, label, extra = {}) => ({
  provider,
  label,
  installed: true,
  authenticated: true,
  binary_path: `/home/lab/.local/bin/${provider}`,
  path_state: "resolved",
  version: "0.131.0",
  update_warning: null,
  updating: false,
  models: [],
  runtimes: [],
  default_runtime: "default",
  ...extra,
});

function machine() {
  const providers = [
    { provider: "codex", label: "Codex", models: [] },
    { provider: "claude", label: "Claude", models: [] },
    { provider: "agy", label: "Gemini", models: [] },
  ];
  return {
    machine_id: MACHINE,
    name: "gpu-1",
    host: "lab@gpu-1",
    os_account: "lab",
    writable_paths: [],
    hidden_folders: [],
    provider_autocompact: {},
    provider_shell_timeout: {},
    autocompact_providers: [],
    shell_timeout_providers: [],
    hidden_read: { folders: [], scopes: [] },
    projects: [],
    in_use: false,
    deletable: true,
    job_manager: "none",
    jobs_root: "",
    access_hint: "",
    provider_paths: Object.fromEntries(
      providers.map(({ provider }) => [provider, `/home/lab/.local/bin/${provider}`]),
    ),
    providers,
    provider_readiness: Object.fromEntries(
      providers.map(({ provider, label }) => [provider, ready(provider, label)]),
    ),
    compute_probes: { scheduler: null, helper: null },
    agents_run_here: false,
    reach: [],
  };
}

const logins = [
  loginRow({
    provider: "codex",
    label: "Codex",
    login_id: "ana",
    added_by_display_name: "Ana",
    limited_at: "2026-11-27T11:00:00Z",
    limit_resets_at: RESETS_AT,
    limit_reset_estimated: false,
  }),
  loginRow({
    provider: "codex",
    label: "Codex",
    login_id: "bo",
    added_by_display_name: "Bo",
    limited_at: "2026-11-27T11:00:00Z",
    limit_resets_at: RESETS_AT,
    limit_reset_estimated: true,
  }),
];

async function openCard(t, viewport, onUpdate, onPoll = () => null) {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0, hmr: false },
  });
  await server.listen();
  let browser;
  t.after(async () => {
    await browser?.close();
    await server.close();
  });
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error" && !/\b(4\d\d|5\d\d)\b/.test(message.text()))
      errors.push(message.text());
  });
  const state = { record: machine(), calls: [] };
  await page.route("**/api/**", (route) => route.fulfill({ status: 404, json: { detail: "no" } }));
  await page.route("**/api/space/machines", (route) =>
    route.fulfill(onPoll(state) ?? { json: { machines: [state.record] } }),
  );
  await page.route("**/api/providers/logins", (route) => route.fulfill({ json: logins }));
  await page.route("**/api/providers/logins/settings", (route) =>
    route.fulfill({ json: settingsFor() }),
  );
  await page.route("**/api/space/machines/*/providers/*/update", async (route) => {
    const request = route.request();
    const provider = new URL(request.url()).pathname.split("/").at(-2);
    state.calls.push([request.method(), provider]);
    return route.fulfill(onUpdate(provider, request.method(), state));
  });
  await page.goto(
    `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/providerUpdate.html`,
  );
  await page.locator(".provider-path-row").first().waitFor({ state: "attached" });
  await page.evaluate(() => {
    for (const details of document.querySelectorAll("details")) details.open = true;
  });
  return { page, state, errors };
}

const row = (page, provider) => page.locator(`.provider-path-row[data-provider="${provider}"]`);

async function chooseUpdate(page, provider, label) {
  await row(page, provider)
    .getByRole("button", { name: `${label} actions` })
    .click();
  await page.getByRole("menuitem", { name: "Update" }).click();
}

async function assertNoSideScroll(page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  assert.ok(overflow <= 0, `no horizontal scroll (${overflow}px over)`);
}

for (const [width, height] of [
  [1440, 900],
  [390, 844],
]) {
  test(`Update runs from the provider menu and the card rereads when it ends (${width}px)`, async (t) => {
    let polls = 0;
    const { page, state, errors } = await openCard(
      t,
      { width, height },
      (provider, method, current) => {
        const readiness = current.record.provider_readiness[provider];
        if (provider === "codex")
          return { status: 409, json: { detail: "2 turns are using Codex here." } };
        assert.equal(method, "POST");
        readiness.updating = true;
        return { status: 202, json: { ok: true } };
      },
      (current) => {
        const active = Object.entries(current.record.provider_readiness).find(
          ([, readiness]) => readiness.updating,
        );
        if (!active) return null;
        const [provider, readiness] = active;
        polls += 1;
        if (polls === 1) return { status: 503, json: { detail: "Card temporarily unavailable" } };
        if (polls < 3) return null;
        readiness.updating = false;
        if (provider === "agy") readiness.update_warning = "npm ERR! EACCES";
        else readiness.version = "0.132.0";
        return null;
      },
    );
    const claude = row(page, "claude");
    await chooseUpdate(page, "claude", "Claude");
    await claude.locator(".provider-path-state.pending").waitFor();
    assert.equal(await claude.getAttribute("data-updating"), "true");
    assert.equal(await claude.getByRole("button", { name: "Claude actions" }).count(), 0);
    assert.equal(await claude.getByRole("status").count(), 1);
    if (shots) {
      mkdirSync(shots, { recursive: true });
      await page.screenshot({
        path: `${shots}/provider-rows-updating-${width}.png`,
        fullPage: true,
      });
    }
    await claude.locator(".provider-path-state.ready").waitFor({ timeout: 10_000 });
    assert.equal(await claude.getAttribute("data-updating"), null);
    assert.equal(state.record.provider_readiness.claude.version, "0.132.0");

    polls = 0;
    const gemini = row(page, "agy");
    await chooseUpdate(page, "agy", "Gemini");
    await gemini.locator(".provider-path-state.pending").waitFor();
    await gemini.locator(".provider-path-state.warning").waitFor({ timeout: 10_000 });
    assert.equal(
      await gemini.locator(".provider-path-state").getAttribute("title"),
      "npm ERR! EACCES",
    );

    const codex = row(page, "codex");
    await chooseUpdate(page, "codex", "Codex");
    await codex.locator(".provider-path-state.warning").waitFor();
    assert.equal(
      await codex.locator(".provider-path-state").getAttribute("title"),
      "2 turns are using Codex here.",
    );
    assert.deepEqual(state.calls, [
      ["POST", "claude"],
      ["POST", "agy"],
      ["POST", "codex"],
    ]);

    if (width < 560) {
      const name = await claude.locator("strong").boundingBox();
      const tag = await claude.locator(".provider-path-state").boundingBox();
      const menu = await claude.locator(".provider-path-menu").boundingBox();
      const input = await claude.locator("input").boundingBox();
      const rowBox = await claude.boundingBox();
      for (const box of [tag, menu])
        assert.ok(box.y < name.y + name.height && name.y < box.y + box.height, "one first line");
      assert.ok(input.y >= menu.y + menu.height - 1, "the path sits below");
      assert.ok(input.width > rowBox.width * 0.8, "the path takes the row's width");
    }
    await assertNoSideScroll(page);
    if (shots)
      await page.screenshot({ path: `${shots}/provider-rows-${width}.png`, fullPage: true });
    assert.deepEqual(errors, []);
  });
}

for (const width of [1440, 390])
  test(`a limited sign-in card names when its limit ends (${width}px)`, async (t) => {
    const { page } = await openCard(t, { width, height: 844 }, () => ({ status: 404 }));
    const card = (id) =>
      page.locator(
        `.sign-in-card[data-login-id="${id}"][data-card-state="limited"] .sign-in-state`,
      );
    await card("ana").waitFor();
    const clock = await page.evaluate((iso) => {
      const at = new Date(iso);
      return `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
    }, RESETS_AT);
    assert.ok((await card("ana").textContent()).includes(clock));
    assert.ok(!(await card("bo").textContent()).includes(clock));
    await assertNoSideScroll(page);
  });
