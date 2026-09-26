import assert from "node:assert/strict";
import test from "node:test";

test("Runs warns once per checked route that cannot run jobs, only for offered routes", async () => {
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
    const { ComputeRouteNotice } = await server.ssrLoadModule(
      "/src/components/ComputeRouteNotice.tsx",
    );
    const probe = (ready) => ({ ready, diagnostic: "diagnostic", required_action: "fix-it" });
    const machine = (alias, jobManager, probes) => ({
      alias,
      compute: jobManager ? { job_manager: jobManager, jobs_root: "" } : null,
      compute_probes: { scheduler: null, helper: null, ...probes },
    });
    const render = (machines) =>
      renderToStaticMarkup(
        React.createElement(ComputeRouteNotice, { machines, onOpenSettings: () => {} }),
      );
    const notices = (markup) => markup.match(/role="status"/g)?.length ?? 0;

    assert.equal(notices(render([machine("gpu", "slurm", { helper: probe(false) })])), 1);
    assert.match(render([machine("gpu", "slurm", { helper: probe(false) })]), /fix-it/);
    assert.equal(
      notices(render([machine("gpu", "slurm", { scheduler: probe(false), helper: probe(false) })])),
      2,
    );
    // A stale scheduler row on a machine without a job manager is not offered.
    assert.equal(notices(render([machine("laptop", null, { scheduler: probe(false) })])), 0);
    assert.equal(notices(render([machine("laptop", null, { helper: probe(true) })])), 0);
    assert.equal(notices(render([machine("laptop", null, {})])), 0);
  } finally {
    await server.close();
  }
});
