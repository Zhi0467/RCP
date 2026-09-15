import assert from "node:assert/strict";
import test from "node:test";

import {
  accountLabel,
  resumedNote,
  signInNote,
  tokenNote,
  signedOutNote,
} from "../src/providerLogins.ts";

test("the account label names the machine account and the project aliases that use it", () => {
  assert.equal(accountLabel({ host: "", machines: [] }, "team"), "Team server");
  assert.equal(
    accountLabel({ host: "", machines: ["local"] }, "personal"),
    "Local machine (local)",
  );
  assert.equal(
    accountLabel({ host: "gpu.example", machines: ["gpu", "gpu.example"] }, "team"),
    "gpu.example (gpu)",
  );
});

test("the token note is an estimate that never contains the token", () => {
  const now = new Date("2026-09-14T12:00:00Z");
  const token = {
    pasted_at: "2026-09-14T10:00:00Z",
    pasted_by: "member",
    verified_at: "2026-09-14T10:00:05Z",
    estimated_expiry_at: "2027-08-15T10:00:00Z",
  };
  const note = tokenNote(token, now);
  assert.match(note, /saved by member, verified/);
  assert.match(note, /about 334 more days/);
  assert.match(tokenNote({ ...token, verified_at: null }, now), /not verified yet/);
  assert.match(
    tokenNote({ ...token, estimated_expiry_at: "2026-09-01T00:00:00Z" }, now),
    /estimated lifetime ended/,
  );
});

test("the sign-in note follows the device-code flow", () => {
  const base = {
    login_id: "login",
    provider: "codex",
    host: "",
    state: "pending",
    user_code: null,
    verification_url: null,
    detail: null,
    started_at: "2026-09-14T10:00:00Z",
    started_by: "member",
    finished_at: null,
    resumed: null,
  };
  assert.match(signInNote(base), /device code/);
  assert.match(
    signInNote({ ...base, user_code: "ABCD-EFGH", verification_url: "https://auth.example/d" }),
    /enter this code/,
  );
  assert.match(signInNote({ ...base, state: "succeeded" }), /verified/);
  assert.equal(
    signInNote({ ...base, state: "failed", detail: "denied" }),
    "Sign-in failed: denied",
  );
});

test("verification reports checks without claiming every item launched", () => {
  assert.equal(resumedNote({ checked: 0 }), "Verified.");
  assert.equal(resumedNote({ checked: 1 }), "Verified. Rechecked 1 item for resumption.");
  assert.equal(resumedNote({ checked: 3 }), "Verified. Rechecked 3 items for resumption.");
});

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
    assert.match(device, /Sign in with device code/);
    assert.doesNotMatch(device, /type="password"|Claude|Codex/);
    const token = render({ sign_in_methods: ["token_entry"] });
    assert.match(token, /type="password"/);
    assert.match(token, /Paste the test provider token/);
    assert.doesNotMatch(token, /Sign in with device code|Claude|Codex/);
    const unsupported = render({ sign_in_methods: ["future_method"] });
    assert.doesNotMatch(unsupported, /type="password"|Sign in with device code/);
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

test("a signed-out account reads as one sentence, with no doubled full stop", () => {
  assert.equal(
    signedOutNote({ label: "Codex", provider: "codex", host: "", detail: null }),
    "Codex is signed out.",
  );
  assert.equal(
    signedOutNote({
      label: "Codex",
      provider: "codex",
      host: "gpu-1",
      detail: "The sign-in was canceled before it completed.",
    }),
    "Codex is signed out on gpu-1. The sign-in was canceled before it completed.",
  );
});
