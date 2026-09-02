import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { shouldRequestProviderReadiness } = await server.ssrLoadModule("/src/App.tsx");
const { AgentConfigControls } = await server.ssrLoadModule(
  "/src/components/AgentConfigControls.tsx",
);

after(() => server.close());

test("a project with no readiness retries after its live request settles", () => {
  const missing = {};
  assert.equal(shouldRequestProviderReadiness(missing, false), true);
  assert.equal(shouldRequestProviderReadiness(missing, true), false);
  assert.equal(shouldRequestProviderReadiness(missing, false), true);
  assert.equal(
    shouldRequestProviderReadiness(
      { local: { codex: { provider: "codex", installed: true, authenticated: true } } },
      false,
    ),
    false,
  );
});

const project = {
  provider_readiness: {},
  machines: [{ alias: "local", host: "" }],
};
const value = { provider: "codex", model: "", reasoning: "medium", run_on: "local" };

test("missing readiness is called checking only while a request is actually pending", () => {
  const checking = renderToStaticMarkup(
    React.createElement(AgentConfigControls, {
      project,
      value,
      onChange() {},
      readinessPending: true,
    }),
  );
  const failed = renderToStaticMarkup(
    React.createElement(AgentConfigControls, {
      project,
      value,
      onChange() {},
      readinessError: "The readiness request failed.",
    }),
  );

  assert.match(checking, /Checking codex on this machine/);
  assert.doesNotMatch(checking, /The readiness request failed/);
  assert.match(failed, /The readiness request failed/);
  assert.doesNotMatch(failed, /Checking codex on this machine/);
});
