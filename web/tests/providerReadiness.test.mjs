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
const { AgentConfigControls, launchProviderReady, settleReadinessRefresh } =
  await server.ssrLoadModule("/src/core/AgentConfigControls.tsx");

after(() => server.close());

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

  assert.doesNotMatch(checking, /The readiness request failed/);
  assert.match(failed, /The readiness request failed/);
});

/** Render the badge for one authenticated Claude readiness shape. */
function readinessMarkup(extra, props = {}) {
  return renderToStaticMarkup(
    React.createElement(AgentConfigControls, {
      ...props,
      project: {
        ...project,
        provider_readiness: {
          local: {
            claude: {
              provider: "claude",
              label: "Claude",
              installed: true,
              authenticated: true,
              models: [],
              ...extra,
            },
          },
        },
      },
      value: { ...value, provider: "claude" },
      onChange() {},
    }),
  );
}

test("authenticated providers still show a failed Work precondition", () => {
  const work_like_reason = "Claude Work sandbox unavailable: bubblewrap (bwrap) not installed.";
  const markup = readinessMarkup({ work_like_available: false, work_like_reason });

  assert.ok(markup.includes(work_like_reason));
  assert.match(markup, /agent-readiness warning/);
});

test("a profile that never launches Work ignores the Work precondition", () => {
  // The paper coach launches paper_readonly, so a missing Work sandbox is not
  // its problem and must not make its card read as unavailable.
  const markup = readinessMarkup(
    {
      work_like_available: false,
      work_like_reason: "Claude Work sandbox unavailable: bubblewrap (bwrap) not installed.",
    },
    { workLikeCapable: false },
  );

  assert.match(markup, /agent-readiness ready/);

  assert.doesNotMatch(markup, /bubblewrap/);
});

test("a benign readiness note does not make a working provider read as broken", () => {
  // `reason` carries notes as well as faults, so only the backend's own Work
  // decision may turn the badge into a warning.
  const markup = readinessMarkup({
    reason: "claude was found at /usr/local/bin/claude; using the discovered path.",
    work_like_available: true,
  });

  assert.match(markup, /agent-readiness ready/);
});

test("a launch is refused for a missing login, and a Work-like one for a failed Work probe", () => {
  const withReadiness = (readiness) => ({
    ...project,
    provider_readiness: { local: { codex: { provider: "codex", ...readiness } } },
  });
  const ready = { installed: true, authenticated: true };
  const workFailed = { ...ready, work_like_available: false };

  assert.equal(launchProviderReady(project, value, true), true);
  assert.equal(launchProviderReady(withReadiness({ installed: true }), value), false);
  assert.equal(launchProviderReady(withReadiness(workFailed), value), true);
  assert.equal(launchProviderReady(withReadiness(workFailed), value, true), false);
  assert.equal(launchProviderReady(withReadiness(ready), value, true), true);
  // A continued session keeps its bound login; only the server can refuse it.
  assert.equal(launchProviderReady(withReadiness({ installed: true }), value, false, true), true);
  assert.equal(launchProviderReady(withReadiness({ installed: false }), value, false, true), false);
});

test("the provider re-check consumes the visible App-owned rejection", async () => {
  await settleReadinessRefresh(async () => {
    throw new Error("shown through readinessError");
  });
});
