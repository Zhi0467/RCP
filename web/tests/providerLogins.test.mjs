import assert from "node:assert/strict";
import test from "node:test";
import { HOST, everyState, loginRow, settingsFor } from "./fixtures/signInRows.mjs";

const model = await import("../src/projects/providerLoginsModel.ts");

test("each stored setting draws its trays, switches and swap, and switches store the doc's values", () => {
  const { trayLayout, switchedSetting } = model;
  const drawn = (value) => {
    const layout = trayLayout(value);
    return [layout.order.join(","), layout.on.own, layout.on.shared, layout.locked, layout.swap];
  };
  assert.deepEqual(drawn("own_then_shared"), ["own,shared", true, true, null, "shared_then_own"]);
  assert.deepEqual(drawn("shared_then_own"), ["shared,own", true, true, null, "own_then_shared"]);
  assert.deepEqual(drawn("own"), ["own,shared", true, false, "own", null]);
  assert.deepEqual(drawn("shared"), ["shared,own", false, true, "shared", null]);

  // Switch Yours off, back on, then swap.
  let value = "own_then_shared";
  value = switchedSetting(value, "own", false);
  assert.equal(value, "shared");
  value = switchedSetting(value, "own", true);
  assert.equal(value, "shared_then_own");
  value = trayLayout(value).swap;
  assert.equal(value, "own_then_shared");

  assert.equal(switchedSetting("own_then_shared", "shared", false), "own");
  assert.equal(switchedSetting("own", "shared", true), "own_then_shared");
  // The last group that is on cannot turn off.
  assert.equal(switchedSetting("own", "own", false), null);
  assert.equal(switchedSetting("shared", "shared", false), null);
});

test("a card shows the first state that applies: draining, signing in, signed out, limited, ready", () => {
  const all = {
    draining_since: "t",
    sign_in: { state: "pending" },
    state: "signed_out",
    limited_at: "t",
  };
  const order = [];
  let row = all;
  for (const drop of ["draining_since", "sign_in", "state", "limited_at", null]) {
    order.push(model.cardState(row));
    if (drop === "state") row = { ...row, state: "signed_in" };
    else if (drop) row = { ...row, [drop]: null };
  }
  assert.deepEqual(order, ["draining", "signing_in", "signed_out", "limited", "ready"]);
  assert.equal(
    model.cardState({ ...all, draining_since: null, sign_in: { state: "failed" } }),
    "signed_out",
  );
});

test("rows split by host into the viewer's own, shared with the machine sign-in first, and members", () => {
  const rows = [...everyState, loginRow({ host: "other.example", login_id: "elsewhere" })];
  const [claude, codex, opencode] = model.signInsForHost(rows, HOST, "u-me");
  assert.equal(claude.own.login_id, "own-me");
  assert.deepEqual(
    claude.shared.map((row) => row.login_id),
    ["", "ana", "bo", "cy"],
  );
  assert.deepEqual(
    claude.members.map((row) => row.login_id),
    ["own-dee"],
  );
  assert.deepEqual(model.membersSignedIn(claude.members, 4), { signedIn: 1, of: 4 });
  assert.equal(codex.own, null);
  assert.equal(opencode.managed, false);
});

test("API sends login ids, add and remove bodies, and the setting through their routes", async () => {
  const api = await import("../src/core/api.ts");
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push([url, options.method ?? "GET", options.body ? JSON.parse(options.body) : null]);
    return new Response(JSON.stringify({}), { status: 200 });
  };
  try {
    await api.addProviderLogin("codex", HOST, "shared");
    await api.addProviderLogin("claude", "", "own");
    await api.removeProviderLogin("codex", HOST, "ana");
    await api.saveProviderLoginSetting("claude", HOST, "shared_then_own");
    await api.loadProviderLoginSettings();
    await api.startProviderSignIn("codex", HOST, "ana");
    await api.providerSignInStatus("codex", "attempt-1");
    await api.cancelProviderSignIn("codex", "attempt-1");
    await api.saveProviderToken("claude", HOST, "own-me", "private-value");
    await api.verifyProviderLogin("claude", HOST, "");
    await api.signOutProvider("claude", HOST, "own-me");
    assert.deepEqual(calls, [
      ["/api/providers/codex/logins/add", "POST", { host: HOST, kind: "shared" }],
      ["/api/providers/claude/logins/add", "POST", { host: "", kind: "own" }],
      ["/api/providers/codex/logins/remove", "POST", { host: HOST, login_id: "ana" }],
      ["/api/providers/claude/logins/setting", "PUT", { host: HOST, setting: "shared_then_own" }],
      ["/api/providers/logins/settings", "GET", null],
      ["/api/providers/codex/logins/sign-in", "POST", { host: HOST, login_id: "ana" }],
      ["/api/providers/codex/logins/sign-in/attempt-1", "GET", null],
      ["/api/providers/codex/logins/sign-in/attempt-1/cancel", "POST", null],
      [
        "/api/providers/claude/logins/token",
        "POST",
        { host: HOST, login_id: "own-me", token: "private-value" },
      ],
      ["/api/providers/claude/logins/verify", "POST", { host: HOST, login_id: "" }],
      ["/api/providers/claude/logins/sign-out", "POST", { host: HOST, login_id: "own-me" }],
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("live login states replace project rows matched by provider, host and login", async () => {
  const { mergeProviderLogins } = await import("../src/projects/providers.ts");
  const project = [
    loginRow({ state: "signed_out" }),
    loginRow({ login_id: "ana", state: "signed_out" }),
  ];
  const live = [loginRow({ login_id: "ana", state: "signed_in", generation: 4 })];
  assert.deepEqual(
    mergeProviderLogins(project, live).map((row) => [row.login_id, row.state]),
    [
      ["", "signed_out"],
      ["ana", "signed_in"],
    ],
  );
});

async function ssr(run) {
  const { createServer } = await import("vite");
  const React = (await import("react")).default;
  const { renderToStaticMarkup } = await import("react-dom/server");
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    configFile: false,
    logLevel: "silent",
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const modules = {
      ...(await server.ssrLoadModule("/src/projects/ProviderLogins.tsx")),
      ...(await server.ssrLoadModule("/src/projects/ProviderLoginNotice.tsx")),
    };
    await run({ React, render: (element) => renderToStaticMarkup(element), ...modules });
  } finally {
    await server.close();
  }
}

const count = (html, pattern) => (html.match(pattern) ?? []).length;

test("Sign-ins fold draws trays per setting, personal and unmanaged rules, and device-code privacy", () =>
  ssr(({ React, render, SignInsFold }) => {
    const fold = (rows, settings, viewerUserId = "u-me") =>
      render(
        React.createElement(SignInsFold, {
          machineName: "gpu-1",
          host: HOST,
          viewerUserId,
          writesDisabled: false,
          data: {
            rows,
            settings,
            error: null,
            refresh: async () => {},
            saveSetting: async () => {},
          },
        }),
      );
    const panels = (html) =>
      Object.fromEntries(
        html
          .split('data-sign-ins-provider="')
          .slice(1)
          .map((part) => [part.slice(0, part.indexOf('"')), part]),
      );
    const team = fold(everyState, settingsFor("own_then_shared", "shared"));
    const { claude, codex, opencode } = panels(team);

    // Claude: both on, Yours first, swap live; every card state present.
    assert.deepEqual(
      [...claude.matchAll(/data-tray="(\w+)"/g)].map((match) => match[1]),
      ["own", "shared"],
    );
    assert.doesNotMatch(claude, /data-sign-ins-swap=""[^>]*aria-disabled/);
    assert.deepEqual(
      [...claude.matchAll(/data-card-state="(\w+)"/g)].map((match) => match[1]),
      ["ready", "ready", "limited", "signed_out", "draining", "ready"],
    );
    assert.equal(count(claude, /data-sign-in-add="shared"/g), 1);
    assert.equal(count(claude, /data-sign-in-add="own"/g), 0);
    assert.equal(count(claude, /class="sign-ins-members"/g), 1);

    // Codex: only Shared on, so it is drawn first, its switch locked, the swap disabled.
    assert.deepEqual(
      [...codex.matchAll(/data-tray="(\w+)"/g)].map((match) => match[1]),
      ["shared", "own"],
    );
    assert.match(codex, /class="sign-ins-tray off" data-tray="own"/);
    assert.match(codex, /data-tray-switch="shared"[^>]*aria-disabled="true"/);
    assert.doesNotMatch(codex, /data-tray-switch="own"[^>]*aria-disabled/);
    assert.match(codex, /data-sign-ins-swap=""[^>]*aria-disabled="true"/);
    // No own Codex login: Yours offers its add slot.
    assert.equal(count(codex, /data-sign-in-add="own"/g), 1);

    // Device codes reach only the member who started the sign-in.
    assert.equal(count(team, /ABCD-1234/g), 1);
    assert.equal(count(team, /data-sign-in-action="cancel"/g), 1);
    const otherViewer = fold(everyState, settingsFor(), "u-eve");
    assert.equal(count(otherViewer, /ABCD-1234|example\.invalid/g), 0);
    assert.equal(count(otherViewer, /data-sign-in-action="cancel"/g), 0);

    // An unmanaged CLI: its machine card only, no switch, add or ⋯.
    assert.equal(count(opencode, /data-card-state=/g), 1);
    assert.equal(count(opencode, /data-tray|data-sign-in-add|data-sign-in-action="more"/g), 0);

    // Personal: one shared tray, no switches, swap or Members.
    const personal = fold(
      everyState.filter((row) => row.kind === "shared"),
      settingsFor("shared", "shared", true),
    );
    assert.equal(count(personal, /data-tray="shared"/g), 2);
    assert.equal(
      count(personal, /data-tray="own"|data-tray-switch|data-sign-ins-swap|sign-ins-members/g),
      0,
    );
  }));

test("a login notice offers one action, except on another member's own login", () =>
  ssr(({ React, render, ProviderLoginNotice }) => {
    const html = render(
      React.createElement(ProviderLoginNotice, {
        viewerUserId: "u-me",
        onOpenSignIns() {},
        states: [
          loginRow({ state: "signed_out" }),
          loginRow({ state: "signed_out", kind: "own", login_id: "own-me", owner_user_id: "u-me" }),
          loginRow({
            state: "signed_out",
            kind: "own",
            login_id: "own-dee",
            owner_user_id: "u-dee",
            owner_display_name: "Dee",
          }),
          loginRow({ login_id: "ana" }),
        ],
      }),
    );
    assert.deepEqual(
      [...html.matchAll(/data-login-notice="([\w-]+)"/g)].map((match) => match[1]),
      ["open", "open", "other-member"],
    );
    assert.equal(count(html, /data-login-notice-action="open-sign-ins"/g), 2);
    assert.match(html, /provider-login-notice-owner">Dee</);
    // A row is one line: no paragraphs.
    assert.equal(count(html, /<p[ >]/g), 0);
  }));

test("a dismissed sign-in notice stays hidden until that login signs out again", async () => {
  const stored = new Map();
  globalThis.localStorage = {
    getItem: (key) => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, String(value)),
    removeItem: (key) => stored.delete(key),
    key: (index) => [...stored.keys()][index] ?? null,
    get length() {
      return stored.size;
    },
  };
  const { loginNoticeDismissKey: key } = model;
  const banners = "../src/ui/bannerDismissal.ts";
  const signOut = {
    provider: "claude",
    host: "gpu.example",
    login_id: "",
    generation: 3,
    changed_at: "t1",
  };
  try {
    const first = await import(`${banners}?tab=1`);
    assert.equal(first.isBannerDismissed(key(signOut)), false);
    first.dismissBanner(key(signOut));
    // A new page load reads the device's record.
    const reloaded = await import(`${banners}?tab=2`);
    assert.equal(reloaded.isBannerDismissed(key(signOut)), true);
    // Another failure of the same sign-out moves only its time.
    assert.equal(reloaded.isBannerDismissed(key({ ...signOut, changed_at: "t2" })), true);
    assert.equal(reloaded.isBannerDismissed(key({ ...signOut, generation: 5 })), false);
    assert.equal(reloaded.isBannerDismissed(key({ ...signOut, host: "" })), false);
    assert.equal(reloaded.isBannerDismissed(key({ ...signOut, login_id: "ana" })), false);
    // A cleared condition forgets only its own dismissals.
    reloaded.dismissBanner("compute-route:gpu:helper:timeout");
    reloaded.forgetBanners("compute-route:gpu:helper:");
    assert.equal(reloaded.isBannerDismissed("compute-route:gpu:helper:timeout"), false);
    assert.equal(reloaded.isBannerDismissed(key(signOut)), true);
    globalThis.localStorage = {
      getItem: () => {
        throw new Error("unavailable");
      },
      setItem: () => {
        throw new Error("unavailable");
      },
    };
    const blocked = key({ ...signOut, generation: 4 });
    assert.equal(reloaded.isBannerDismissed(blocked), false);
    reloaded.dismissBanner(blocked);
    assert.equal(reloaded.isBannerDismissed(blocked), true);
  } finally {
    delete globalThis.localStorage;
  }
});
