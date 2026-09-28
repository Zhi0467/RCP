import assert from "node:assert/strict";
import test from "node:test";

test("a registered third provider renders only its declared interactions and backend label", async () => {
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
    const { ProviderLoginRow } = await server.ssrLoadModule("/src/components/ProviderLogins.tsx");
    const account = {
      provider: "test-provider",
      label: "Test Research Provider",
      host: "",
      machines: [],
      state: "signed_out",
      generation: 0,
      changed_at: "",
      changed_by: null,
      detail: null,
      token: null,
      sign_in: null,
      sign_in_methods: ["device_code"],
      token_instructions: "Paste the test provider token",
    };
    const render = (overrides = {}) =>
      renderToStaticMarkup(
        React.createElement(ProviderLoginRow, {
          account: { ...account, ...overrides },
          spaceKind: "personal",
          writesDisabled: false,
          memberName: (id) => id,
          onChanged: async () => {},
        }),
      );
    const device = render();
    assert.match(device, /Test Research Provider/);

    assert.doesNotMatch(device, /type="password"|Claude|Codex/);
    const token = render({ sign_in_methods: ["token_entry"] });
    assert.match(token, /type="password"/);
    assert.match(token, /Paste the test provider token/);
    assert.doesNotMatch(token, /Claude|Codex/);
    // Nothing is saved yet, so rechecking could only fail: one action, not two.

    const saved = render({
      sign_in_methods: ["token_entry"],
      token: {
        pasted_at: "2026-09-14T10:00:00Z",
        pasted_by: "member",
        verified_at: "2026-09-14T10:00:05Z",
        estimated_expiry_at: "2027-08-15T10:00:00Z",
      },
    });

    // A device-code account can always be rechecked; its credential is native.

    assert.match(device, /data-provider-action="sign-in"/);
    assert.match(device, /data-provider-action="verify"/);
    assert.doesNotMatch(token, /data-provider-action=/);
    assert.match(token, /<form class="provider-login-token"/);
    assert.match(token, /type="submit"/);
    assert.match(saved, /data-provider-action="verify"/);
    assert.doesNotMatch(saved, /data-provider-action="sign-in"/);
    assert.match(saved, /<form class="provider-login-token"/);
    const unsupported = render({ sign_in_methods: ["future_method"] });
    assert.match(unsupported, /data-provider-action="verify"/);
    assert.doesNotMatch(unsupported, /data-provider-action="sign-in"|<form/);
    assert.doesNotMatch(unsupported, /type="password"/);
  } finally {
    await server.close();
  }
});

test("shared account API sends a third provider through the generic routes", async () => {
  const { startProviderSignIn, providerSignInStatus, saveProviderToken } =
    await import("../src/api.ts");
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push([url, options]);
    return new Response(JSON.stringify({}), { status: 200 });
  };
  try {
    await startProviderSignIn("test-provider", "remote");
    await providerSignInStatus("test-provider", "operation");
    await saveProviderToken("test-provider", "remote", "private-value");
    assert.deepEqual(
      calls.map(([url]) => url),
      [
        "/api/providers/test-provider/logins/sign-in",
        "/api/providers/test-provider/logins/sign-in/operation",
        "/api/providers/test-provider/logins/token",
      ],
    );
    assert.deepEqual(JSON.parse(calls[2][1].body), { host: "remote", token: "private-value" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("declared provider controls invoke the matching provider operations", async () => {
  const { createServer } = await import("vite");
  const { chromium } = await import("playwright");
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "error",
    server: { host: "127.0.0.1", port: 0, hmr: false },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    // Settle the fixture's initial pending sign-in before exercising new actions.
    await page.route("**/api/providers/codex/logins/sign-in/login-1", (route) =>
      route.fulfill({ json: { login_id: "login-1", state: "succeeded" } }),
    );
    const calls = [];
    await page.route("**/api/providers/test-provider/logins/*", (route) => {
      calls.push({
        operation: route.request().url().split("/").at(-1),
        method: route.request().method(),
        body: route.request().postDataJSON(),
      });
      return route.fulfill({ json: { resumed: { checked: 0 } } });
    });
    await page.goto(
      `http://127.0.0.1:${server.httpServer.address().port}/tests/fixtures/providerLoginRow.html`,
    );
    await page.waitForFunction(() => window.loginChanges === 1);
    await page.evaluate(() =>
      window.setLoginAccount({
        provider: "test-provider",
        host: "remote",
        sign_in: null,
      }),
    );
    const invoke = async (selector, operation, body = { host: "remote" }) => {
      const changes = await page.evaluate(() => window.loginChanges);
      await page.locator(selector).click();
      await page.waitForFunction((previous) => window.loginChanges > previous, changes);
      assert.deepEqual(calls.at(-1), { operation, method: "POST", body });
    };
    await invoke('[data-provider-action="sign-in"]', "sign-in");
    await invoke('[data-provider-action="verify"]', "verify");
    await page.evaluate(() => window.setLoginAccount({ sign_in_methods: ["token_entry"] }));
    await page.locator(".provider-login-token input").fill("  private-value  ");
    assert.equal(await page.locator("[data-provider-action]").count(), 0);
    await invoke('.provider-login-token button[type="submit"]', "token", {
      host: "remote",
      token: "private-value",
    });
    await page.evaluate(() => {
      const now = new Date().toISOString();
      window.setLoginAccount({
        token: {
          pasted_at: now,
          pasted_by: "member",
          verified_at: now,
          estimated_expiry_at: null,
        },
      });
    });
    await invoke('[data-provider-action="verify"]', "verify");
    await page.locator(".provider-login-token input").fill("replacement-token");
    await invoke('.provider-login-token button[type="submit"]', "token", {
      host: "remote",
      token: "replacement-token",
    });
    await page.evaluate(() =>
      window.setLoginAccount({
        sign_in_methods: ["future_method"],
        token: null,
      }),
    );
    await page.locator(".provider-login-token").waitFor({ state: "detached" });
    assert.equal(await page.locator('[data-provider-action="sign-in"]').count(), 0);
    await invoke('[data-provider-action="verify"]', "verify");
    assert.equal(calls.length, 6);
    assert.deepEqual(errors, []);
  } finally {
    await browser?.close();
    await server.close();
  }
});
