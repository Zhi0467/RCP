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
const {
  advanceProjectReadinessGeneration,
  projectReadinessRequestCanApply,
  shouldRequestProviderReadiness,
} = await server.ssrLoadModule("/src/App.tsx");
const { AgentConfigControls, settleReadinessRefresh } = await server.ssrLoadModule(
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

test("a compute-settings save invalidates an older deferred readiness response", async () => {
  const generations = new Map();
  const requestGeneration = generations.get("project") ?? 0;
  let complete;
  const deferred = new Promise((resolve) => {
    complete = resolve;
  });
  let applied = { compute_status: {} };
  const consume = deferred.then((readiness) => {
    if (projectReadinessRequestCanApply(generations, "project", requestGeneration)) {
      applied = readiness;
    }
  });

  advanceProjectReadinessGeneration(generations, "project");
  applied = { compute_status: {} }; // The save response clears the old target's status.
  await assert.rejects(Promise.reject(new Error("new probe failed")), /new probe failed/);
  complete({ compute_status: { local: { gpu: { status_label: "stale green" } } } });
  await consume;

  assert.deepEqual(applied, { compute_status: {} });
});

test("the provider re-check consumes the visible App-owned rejection", async () => {
  await settleReadinessRefresh(async () => {
    throw new Error("shown through readinessError");
  });
});
