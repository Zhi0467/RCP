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
  invalidateProjectReadinessGenerations,
  projectReadinessFailureApplies,
  projectReadinessFailureState,
  currentProjectReadinessGeneration,
  projectReadinessResponseApplies,
  projectReadinessUpdate,
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

/** Consume one deferred readiness response the way the App-owned request does. */
function deferredReadiness(generations, projectId) {
  const requestGeneration = currentProjectReadinessGeneration(generations, projectId);
  let complete;
  const response = new Promise((resolve) => {
    complete = resolve;
  });
  return {
    complete,
    applied: response.then((readiness) => {
      const applies = projectReadinessResponseApplies(generations, projectId, requestGeneration);
      if (!applies.provider && !applies.compute) return null;
      return projectReadinessUpdate(readiness, applies);
    }),
  };
}

const probedCompute = { local: { gpu: { status_label: "Reachable" } } };
const probedProviders = { local: { codex: { provider: "codex", installed: true } } };
const readinessResponse = {
  compute_status: probedCompute,
  provider_readiness: probedProviders,
  providers: probedProviders.local,
  provider_skill_inventories: {},
};

test("a compute-settings save invalidates an older deferred readiness response", async () => {
  const generations = new Map();
  const probe = deferredReadiness(generations, "project");

  // The save response clears the old target's status.
  invalidateProjectReadinessGenerations(generations, "project", {
    provider: false,
    compute: false,
  });
  probe.complete(readinessResponse);

  assert.equal(await probe.applied, null);
});

test("a provider resolve keeps a compute probe that answers afterwards", async () => {
  const generations = new Map();
  const probe = deferredReadiness(generations, "project");

  // Resolve invalidates the provider slice only; the probe is still in flight.
  invalidateProjectReadinessGenerations(generations, "project", {
    provider: false,
    compute: true,
  });
  probe.complete(readinessResponse);

  assert.deepEqual(await probe.applied, { compute_status: probedCompute });
});

test("a compute-settings save drops the matrix without dropping provider readiness", async () => {
  const generations = new Map();
  const probe = deferredReadiness(generations, "project");

  invalidateProjectReadinessGenerations(generations, "project", {
    provider: true,
    compute: false,
  });
  probe.complete(readinessResponse);

  assert.deepEqual(await probe.applied, {
    provider_readiness: probedProviders,
    providers: probedProviders.local,
    provider_skill_inventories: {},
  });
});

test("a replaced readiness request stays silent when it fails", () => {
  const current = { provider: true, compute: true };
  const superseded = { provider: false, compute: false };
  const partial = { provider: false, compute: true };

  // A replaced request cannot write pending, because the finally that clears
  // pending runs only for the registered request.
  assert.equal(projectReadinessFailureApplies(false, current), false);
  assert.equal(projectReadinessFailureApplies(false, partial), false);
  assert.equal(projectReadinessFailureApplies(true, superseded), false);
  assert.equal(projectReadinessFailureApplies(true, partial), true);
  assert.equal(projectReadinessFailureApplies(true, current), true);
});

test("a failed readiness response reports only for the slices it still owns", () => {
  const generations = new Map();
  const requestGeneration = currentProjectReadinessGeneration(generations, "project");
  // A provider resolve landed while the compute probe was in flight.
  invalidateProjectReadinessGenerations(generations, "project", {
    provider: false,
    compute: true,
  });
  const applies = projectReadinessResponseApplies(generations, "project", requestGeneration);

  assert.deepEqual(projectReadinessFailureState(undefined, applies, "probe failed"), {
    pending: true,
    providerError: null,
    computeError: "probe failed",
  });
  assert.deepEqual(
    projectReadinessFailureState(
      { pending: true, providerError: "resolve failed", computeError: null },
      applies,
      "probe failed",
    ),
    { pending: true, providerError: "resolve failed", computeError: "probe failed" },
  );
  // The mirror: a compute-configuration save superseded the compute slice.
  const afterSave = new Map();
  const saveRequest = currentProjectReadinessGeneration(afterSave, "project");
  invalidateProjectReadinessGenerations(afterSave, "project", { provider: true, compute: false });

  assert.deepEqual(
    projectReadinessFailureState(
      undefined,
      projectReadinessResponseApplies(afterSave, "project", saveRequest),
      "readiness failed",
    ),
    { pending: true, providerError: "readiness failed", computeError: null },
  );
});

test("the provider re-check consumes the visible App-owned rejection", async () => {
  await settleReadinessRefresh(async () => {
    throw new Error("shown through readinessError");
  });
});
