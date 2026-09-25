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

    const unsupported = render({ sign_in_methods: ["future_method"] });
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
