import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import test from "node:test";
import { createServer } from "vite";
import { chromium } from "playwright";
import { HOST, everyState, loginRow, settingsFor, signIn } from "./fixtures/signInRows.mjs";

/**
 * Serve the Sign-ins fixture with a routed login API. `state.rows` and
 * `state.settings` are what the next list read answers; `calls` records every
 * action request with its body.
 */
async function openSignIns(t, { query = "", viewport, rows, settings, onAction } = {}) {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0, hmr: false },
  });
  await server.listen();
  const browser = await chromium.launch({ headless: true });
  t.after(async () => {
    await browser.close();
    await server.close();
  });
  const page = await browser.newPage(viewport ? { viewport } : undefined);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error" && !/\b(4\d\d|5\d\d)\b/.test(message.text()))
      errors.push(message.text());
  });
  const state = { rows: rows ?? everyState, settings: settings ?? settingsFor() };
  const calls = [];
  await page.route("**/api/providers/logins", (route) => route.fulfill({ json: state.rows }));
  await page.route("**/api/providers/logins/settings", (route) =>
    route.fulfill({ json: state.settings }),
  );
  await page.route("**/api/providers/*/logins/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const call = {
      path: url.pathname.replace(/^\/api\/providers\//, ""),
      method: request.method(),
      body: request.postData() ? request.postDataJSON() : null,
    };
    calls.push(call);
    const answer = (await onAction?.(call, state)) ?? { json: { resumed: { checked: 0 } } };
    return route.fulfill(answer);
  });
  await page.goto(
    `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/signIns.html?${query}`,
  );
  await page.locator(".sign-ins-panel").first().waitFor({ state: "attached" });
  await page.evaluate(() => {
    for (const details of document.querySelectorAll("details")) details.open = true;
  });
  return { page, calls, state, errors };
}

const panel = (page, provider) => page.locator(`[data-sign-ins-provider="${provider}"]`);
const card = (page, provider, loginId) =>
  panel(page, provider).locator(`.sign-in-card[data-login-id="${loginId}"]`);

test("switches and swap write the setting at once and redraw the tray order", async (t) => {
  const { page, calls, state, errors } = await openSignIns(t, {
    onAction: (call, current) => {
      if (call.path.endsWith("/setting")) {
        current.settings = settingsFor(call.body.setting, "shared");
        return { json: { ...current.settings.settings[0], resumed: { checked: 0 } } };
      }
    },
  });
  const claude = panel(page, "claude");
  const trayOrder = () =>
    claude.locator("[data-tray]").evaluateAll((trays) => trays.map((tray) => tray.dataset.tray));
  const settingIs = (value) =>
    page.waitForFunction(
      (expected) =>
        document.querySelector('[data-sign-ins-provider="claude"]')?.dataset.setting === expected,
      value,
    );
  assert.deepEqual(await trayOrder(), ["own", "shared"]);

  await claude.locator('[data-tray-switch="own"]').click();
  await settingIs("shared");
  assert.deepEqual(await trayOrder(), ["shared", "own"]);
  assert.equal(
    await claude.locator('[data-tray="own"]').getAttribute("class"),
    "sign-ins-tray off",
  );
  // One group on: its switch is locked and the swap writes nothing, even when pressed.
  await claude.locator('[data-tray-switch="shared"]').click({ force: true });
  await claude.locator("[data-sign-ins-swap]").click({ force: true });
  assert.equal(await claude.locator('[data-tray-switch="shared"]').isChecked(), true);

  await claude.locator('[data-tray-switch="own"]').click();
  await settingIs("shared_then_own");
  assert.deepEqual(await trayOrder(), ["shared", "own"]);
  await claude.locator("[data-sign-ins-swap]").click();
  await settingIs("own_then_shared");
  assert.deepEqual(await trayOrder(), ["own", "shared"]);

  assert.deepEqual(
    calls.map((call) => [call.path, call.method, call.body]),
    ["shared", "shared_then_own", "own_then_shared"].map((setting) => [
      "claude/logins/setting",
      "PUT",
      { host: HOST, setting },
    ]),
  );
  assert.equal(state.settings.settings[0].setting, "own_then_shared");
  assert.deepEqual(errors, []);
});

test("a refused setting returns the drawn value and offers the write again", async (t) => {
  let refuse = true;
  const { page, calls, errors } = await openSignIns(t, {
    onAction: (call, current) => {
      if (refuse) return { status: 503, json: { detail: "unavailable" } };
      current.settings = settingsFor(call.body.setting, "shared");
    },
  });
  const claude = panel(page, "claude");
  await claude.locator('[data-tray-switch="shared"]').click();
  await page.locator('[data-sign-ins-provider="claude"].failed').waitFor();
  assert.equal(await claude.getAttribute("data-setting"), "own_then_shared");
  refuse = false;
  await claude.locator(':scope > header [aria-label="Retry"]').click();
  await page.waitForFunction(
    () => document.querySelector('[data-sign-ins-provider="claude"]')?.dataset.setting === "own",
  );
  assert.deepEqual(
    calls.map((call) => call.body.setting),
    ["own", "own"],
  );
  assert.deepEqual(errors, []);
});

test("add slots add shared and own logins; Claude opens token entry on the new login", async (t) => {
  const own = loginRow({
    kind: "own",
    login_id: "own-new",
    owner_user_id: "u-me",
    state: "signed_out",
  });
  const { page, calls, state, errors } = await openSignIns(t, {
    rows: everyState.filter((row) => !(row.provider === "claude" && row.kind === "own")),
    onAction: (call, current) => {
      if (call.path === "claude/logins/add") {
        current.rows = [...current.rows, own];
        return { json: { login_id: "own-new", state: own, sign_in: null } };
      }
      if (call.path === "codex/logins/add")
        return { json: { login_id: "new", state: {}, sign_in: signIn({ login_id: "new" }) } };
    },
  });
  await panel(page, "codex").locator('[data-sign-in-add="shared"]').click();
  await panel(page, "claude").locator('[data-sign-in-add="own"]').click();
  const token = card(page, "claude", "own-new").locator('.sign-in-token input[type="password"]');
  await token.waitFor();
  await token.fill("  private-value  ");
  await card(page, "claude", "own-new").locator('button[type="submit"]').click();
  await page.waitForFunction(() => window.loginChanges >= 3);
  assert.deepEqual(
    calls.map((call) => [call.path, call.body]),
    [
      ["codex/logins/add", { host: HOST, kind: "shared" }],
      ["claude/logins/add", { host: HOST, kind: "own" }],
      ["claude/logins/token", { host: HOST, login_id: "own-new", token: "private-value" }],
    ],
  );
  assert.ok(state.rows.includes(own));
  assert.deepEqual(errors, []);
});

test("card actions sit behind ⋯; removing from shared confirms with the bound count", async (t) => {
  const { page, calls, errors } = await openSignIns(t);
  const bo = card(page, "claude", "bo");
  // Signed out: a Sign in button opens token entry in place for Claude.
  await bo.locator('[data-sign-in-action="sign-in"]').click();
  await bo.locator(".sign-in-token").waitFor();
  await bo.locator("input").press("Escape");
  await bo.locator(".sign-in-token").waitFor({ state: "detached" });
  // A phone has no Escape key, so the entry also has its own cancel.
  await bo.locator('[data-sign-in-action="sign-in"]').click();
  await bo.locator('[data-sign-in-action="cancel-token"]').click();
  await bo.locator(".sign-in-token").waitFor({ state: "detached" });

  await bo.locator('[data-sign-in-action="more"]').click();
  assert.deepEqual(
    await bo
      .locator('[role="menuitem"]')
      .evaluateAll((items) => items.map((item) => item.dataset.signInAction)),
    // Nothing saved yet, so there is nothing to check.
    ["remove"],
  );
  await bo.locator('[data-sign-in-action="remove"]').click();
  assert.equal(await bo.locator("[data-bound-running]").getAttribute("data-bound-running"), "0");
  await bo.locator('[data-sign-in-action="confirm-remove"]').click();
  await page.waitForFunction(() => window.loginChanges === 1);

  // The machine sign-in cannot be removed, and a draining login offers nothing.
  await card(page, "claude", "").locator('[data-sign-in-action="more"]').click();
  assert.deepEqual(
    await card(page, "claude", "")
      .locator('[role="menuitem"]')
      .evaluateAll((items) => items.map((item) => item.dataset.signInAction)),
    ["verify", "sign-out"],
  );
  assert.equal(await card(page, "claude", "cy").locator('[data-sign-in-action="more"]').count(), 0);
  // Another member's own login is read-only.
  assert.equal(await card(page, "claude", "own-dee").locator("[data-sign-in-action]").count(), 0);
  assert.deepEqual(
    calls.map((call) => [call.path, call.body]),
    [["claude/logins/remove", { host: HOST, login_id: "bo" }]],
  );
  assert.deepEqual(errors, []);
});

const codexRows = (sign_in) => [
  loginRow({
    provider: "codex",
    label: "Codex",
    sign_in_methods: ["device_code"],
    state: "signed_out",
    sign_in,
  }),
];
const codexSettings = {
  settings: [{ provider: "codex", host: HOST, setting: "shared", changed_at: null, stored: true }],
  other_member_count: 0,
};

test("a pending sign-in keeps polling past a transient status failure", async (t) => {
  let polls = 0;
  const { page, state, errors } = await openSignIns(t, {
    rows: codexRows(signIn()),
    settings: codexSettings,
    onAction: (call, current) => {
      assert.equal(call.method, "GET");
      polls += 1;
      if (polls === 1) return { status: 503, json: { detail: "Sign-in status unavailable" } };
      current.rows = codexRows(null).map((row) => ({ ...row, state: "signed_in" }));
      return { json: signIn({ state: "succeeded", user_code: null, verification_url: null }) };
    },
  });
  const machine = card(page, "codex", "");
  await page.locator('.sign-in-card.failed [title="Sign-in status unavailable"]').waitFor();
  await page.waitForFunction(() => window.loginChanges === 1);
  await page.locator(".sign-in-card.failed").waitFor({ state: "detached" });
  await page.locator('.sign-in-card[data-card-state="ready"]').waitFor();
  assert.equal(await machine.getAttribute("data-card-state"), "ready");
  assert.equal(polls, 2);
  assert.equal(state.rows[0].state, "signed_in");
  assert.deepEqual(errors, []);
});

test("a failed card action re-reads the list, so a login removed elsewhere leaves", async (t) => {
  const { page } = await openSignIns(t, {
    // Nothing draining or pending, so the list is not already being followed.
    rows: everyState.filter((row) => !row.draining_since && !row.sign_in),
    onAction: (_call, current) => {
      current.rows = current.rows.filter((row) => row.login_id !== "bo");
      return { status: 409, json: { detail: "This login was removed." } };
    },
  });
  const bo = card(page, "claude", "bo");
  await bo.locator('[data-sign-in-action="more"]').click();
  await bo.locator('[data-sign-in-action="remove"]').click();
  await bo.locator('[data-sign-in-action="confirm-remove"]').click();
  await bo.waitFor({ state: "detached", timeout: 3000 });
});

test("a sign-in the server no longer knows stops polling", async (t) => {
  let polls = 0;
  const { page, state } = await openSignIns(t, {
    rows: codexRows(signIn()),
    settings: codexSettings,
    onAction: (_call, current) => {
      polls += 1;
      // A restart settled the attempt; only a fresh list read shows that.
      current.rows = codexRows(null);
      return { status: 404, json: { detail: "Unknown sign-in." } };
    },
  });
  await page.locator('.sign-in-card[data-card-state="signed_out"]').waitFor();
  await page.waitForTimeout(3000);
  assert.equal(polls, 1);
  assert.equal(state.rows[0].sign_in, null);
});

test("a pending sign-in keeps polling while the app re-renders", async (t) => {
  let polls = 0;
  const { page } = await openSignIns(t, {
    query: "rerender=300",
    rows: codexRows(signIn()),
    settings: codexSettings,
    onAction: () => {
      polls += 1;
      return { json: signIn({ state: "succeeded" }) };
    },
  });
  await page.waitForFunction(() => window.loginChanges === 1, null, { timeout: 8000 });
  assert.equal(polls, 1);
});

test("a cancelled sign-in marks its card until the login is signed in again", async (t) => {
  const { page, calls, state, errors } = await openSignIns(t, {
    rows: codexRows(signIn()),
    settings: codexSettings,
    onAction: (call) =>
      call.path.endsWith("/cancel")
        ? {
            json: signIn({
              state: "failed",
              user_code: null,
              verification_url: null,
              detail: "Canceled.",
            }),
          }
        : { json: signIn() },
  });
  const machine = card(page, "codex", "");
  await machine.locator("code").waitFor();
  await machine.locator('[data-sign-in-action="cancel"]').click();
  await page.waitForFunction(() => window.loginChanges >= 1);
  await machine.locator('[aria-label="Retry"]').waitFor();
  assert.equal(await machine.getAttribute("data-card-state"), "signed_out");
  assert.ok(calls.some((call) => call.path === "codex/logins/sign-in/attempt-1/cancel"));
  state.rows = codexRows(null).map((row) => ({ ...row, state: "signed_in", generation: 2 }));
  // The next list read shows the login signed in; the failed attempt is history.
  await machine.locator('[data-sign-in-action="more"]').click();
  await machine.locator('[data-sign-in-action="verify"]').click();
  await page.waitForFunction(
    () =>
      document.querySelector('[data-sign-ins-provider="codex"] .sign-in-card')?.dataset
        .cardState === "ready",
  );
  assert.equal(await machine.locator('[aria-label="Retry"]').count(), 0);
  assert.deepEqual(errors, []);
});

test("every card state fits a phone without sideways scrolling", async (t) => {
  const shots = process.env.RCP_SIGN_INS_SHOTS;
  if (shots) mkdirSync(shots, { recursive: true });
  for (const [name, viewport, theme] of [
    ["desktop", { width: 1180, height: 1100 }, "aqua"],
    ["phone", { width: 390, height: 1400 }, "aqua"],
    ["phone-classic", { width: 390, height: 1400 }, "classic"],
  ]) {
    const { page, errors } = await openSignIns(t, { viewport, query: `theme=${theme}` });
    await card(page, "codex", "").locator("code").waitFor();
    // Token entry is the widest card body.
    await card(page, "claude", "bo").locator('[data-sign-in-action="sign-in"]').click();
    const overflow = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      client: document.documentElement.clientWidth,
    }));
    assert.ok(overflow.scroll <= overflow.client, `${name}: ${JSON.stringify(overflow)}`);
    const swapIcon = await panel(page, "claude")
      .locator("[data-sign-ins-swap] svg")
      .evaluateAll((icons) => icons.map((icon) => getComputedStyle(icon).display !== "none"));
    // ⇄ on a wide screen, ⇅ on a phone.
    assert.deepEqual(swapIcon, name.startsWith("phone") ? [false, true] : [true, false]);
    if (shots) await page.screenshot({ path: `${shots}/sign-ins-${name}.png`, fullPage: true });
    assert.deepEqual(errors, []);
  }
});
