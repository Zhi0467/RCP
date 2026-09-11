import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";

let server;
let browser;
let origin;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
  });
  await server.listen();
  origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
});
after(async () => {
  await browser?.close();
  await server?.close();
});

for (const initialFreshness of ["stale", "fresh"]) {
  test(
    `same-revision reconnect clears the App offline banner from ${initialFreshness} cache`,
    { timeout: 15000 },
    async (t) => {
      const context = await browser.newContext();
      t.after(() => context.close());
      const page = await context.newPage();
      page.setDefaultTimeout(10000);
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      const target = { kind: "main", branch_id: null };
      const providers = {};
      const profile = { provider: "codex", model: "", reasoning: "high", run_on: "local" };
      let connected = false;
      let reconnectLoads = 0;
      let reconnectObservations = 0;
      const reconnectRequested = Promise.withResolvers();
      const project = () => ({
        id: "demo",
        name: "Remote project",
        revision: 1,
        graph_target: target,
        graph_head: { target, revision: 1, transition_id: null },
        snapshot_freshness: connected ? "fresh" : initialFreshness,
        last_remote_sync_at: connected ? "2026-09-01T12:01:00Z" : "2026-09-01T12:00:00Z",
        home_space_id: "space",
        canonical_state: { remote: true, reachable: connected },
        graph_mutation: { available: true, reason: null },
        run_on: "local",
        repositories: [],
        machines: [{ alias: "local", host: "" }],
        project_truth_scope: [],
        default_run_truth_scope: [],
        providers,
        agent_profiles: Object.fromEntries(
          ["seed", "refresh", "node_chat", "project_chat", "paper_coach", "orchestrator"].map(
            (k) => [k, profile],
          ),
        ),
        provider_readiness: { local: providers },
        provider_skill_inventories: {},
        skill_catalog: [],
        skill_defaults: {},
        experiment_control: {},
        attention: {
          pending_proposal_ids: [],
          open_blocker_ids: [],
          decisions_awaiting_choice_ids: [],
          proposal_actions: {},
        },
        counts: {},
        coverage: {
          repositories_seen: [],
          repositories_never_seen: [],
          sessions_read: [],
          sessions_skipped: [],
          note: "",
        },
        graph: {
          revision: 1,
          nodes: {},
          edges: {},
          proposals: {},
          glossary: {},
          ambiguities: {},
          ontology: { types: [], fields: [], relations: [] },
          validation_messages: [],
          belief_transitions: [],
          replay_status: "complete",
        },
        paper: { text: "", sections: [] },
        paper_coach: {},
        validation_messages: [],
      });
      await page.route("**/api/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        let json = [];
        if (path === "/api/health")
          json = { status: "ok", space_id: "space", space_kind: "personal", instance_id: "test" };
        else if (path === "/api/identity")
          json = {
            space_id: "space",
            space_kind: "personal",
            user: { user_id: "human", display_name: "Researcher" },
          };
        else if (path === "/api/projects")
          json = [{ id: "demo", name: "Remote project", revision: 1 }];
        else if (path === "/api/projects/demo" || path.endsWith("/cached")) {
          if (connected) {
            reconnectLoads++;
            reconnectRequested.resolve();
          }
          json = project();
        } else if (path.endsWith("/readiness")) json = project();
        else if (path.endsWith("/revision")) {
          if (connected) reconnectObservations++;
          json = {
            revision: 1,
            snapshot_freshness: project().snapshot_freshness,
            last_remote_sync_at: project().last_remote_sync_at,
            graph_mutation: project().graph_mutation,
          };
        } else if (path.endsWith("/chats")) json = { chats: [], next_cursor: null };
        else if (path.endsWith("/usage")) json = { tasks: [], totals: {}, by_provider: [] };
        await route.fulfill({ json });
      });
      const readinessLoaded = page.waitForResponse((response) =>
        new URL(response.url()).pathname.endsWith("/readiness"),
      );
      await page.goto(`${origin}/#/projects/demo?view=runs`);
      const banner = page.getByText("Canonical state is offline.", { exact: true });
      await banner.waitFor();
      await (await readinessLoaded).finished();
      await page.evaluate(() => new Promise(requestAnimationFrame));
      connected = true;
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await reconnectRequested.promise;
      await banner.waitFor({ state: "detached" });
      assert.ok(reconnectObservations > 0, "heartbeat must observe the reconnection metadata");
      assert.ok(
        reconnectLoads > 0,
        "heartbeat must fetch a project snapshot at the unchanged revision",
      );
      assert.deepEqual(errors, []);
    },
  );
}
