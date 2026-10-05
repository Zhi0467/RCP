import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

test("desktop public personal health renders owner sign-in without protected health", async (t) => {
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    configFile: false,
    logLevel: "silent",
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true },
    plugins: [
      {
        name: "unsigned-owner-identity",
        transform(_code, id) {
          if (!id.endsWith("/desktop/useActorIdentity.ts")) return;
          return `export function useActorIdentity() { return {
          identityReady: true, identityIssue: null, actorIdentityChecked: true,
          ownerSessionRequired: true, actorIdentity: null, authenticatedHealth: null,
          verifiedHealth: { status: "ok", agent_mode: "provider", version: "0.4.11",
            space_id: "personal", space_kind: "personal", instance_id: "instance",
            data_dir_id: "data", owner_kind: "desktop", running_commit: null,
            web_build_id: null, pid: 1, team_shell_protocol: { minimum: 1, maximum: 4 } },
        }; }`;
        },
      },
    ],
  });
  t.after(() => server.close());
  globalThis.document = {
    compatMode: "CSS1Compat",
    visibilityState: "visible",
    documentElement: { dataset: {} },
  };
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.window = {
    __TAURI_INTERNALS__: {},
    location: { hash: "", search: "", pathname: "/" },
    performance: { getEntriesByType: () => [] },
    matchMedia: () => ({ matches: false }),
    localStorage: globalThis.localStorage,
  };
  t.after(() => {
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.localStorage;
  });
  const { default: App } = await server.ssrLoadModule("/src/App.tsx");
  const html = renderToStaticMarkup(React.createElement(App));
  assert.match(html, /<input[^>]*name="owner-code"/);
});
