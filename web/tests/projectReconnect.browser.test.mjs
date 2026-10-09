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

const HEALTH = { status: "ok", space_id: "space", space_kind: "personal", instance_id: "test" };

// Serves one demo project; `state` steers health, connection, and request counts.
async function openDemoProject(t, initialFreshness, options = {}) {
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
  const state = {
    connected: false,
    health: options.health ?? HEALTH,
    signedIn: options.signedIn ?? true,
    requests: [],
    reconnectLoads: 0,
    reconnectObservations: 0,
    reconnectRequested: Promise.withResolvers(),
  };
  const project = () => ({
    id: "demo",
    name: "Remote project",
    revision: 1,
    graph_target: target,
    graph_head: { target, revision: 1, transition_id: null },
    snapshot_freshness: state.connected ? "fresh" : initialFreshness,
    last_remote_sync_at: state.connected ? "2026-09-01T12:01:00Z" : "2026-09-01T12:00:00Z",
    home_space_id: "space",
    canonical_state: { remote: true, reachable: state.connected },
    graph_mutation: { available: true, reason: null },
    run_on: "local",
    repositories: [],
    machines: [{ alias: "local", host: "" }],
    project_truth_scope: [],
    default_run_truth_scope: [],
    providers,
    agent_profiles: Object.fromEntries(
      ["seed", "refresh", "node_chat", "project_chat", "paper_coach", "orchestrator"].map((k) => [
        k,
        profile,
      ]),
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
      decision_prior_choices: {},
    },
    counts: {},
    // Old cached responses must not revive the retired agent-authored warning.
    coverage: {
      repositories_seen: [],
      repositories_never_seen: ["legacy-repository"],
      sessions_read: [],
      sessions_skipped: ["legacy-session"],
      note: "Legacy agent-authored coverage claim.",
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
    state.requests.push(path);
    let json = [];
    if (path === "/api/health" || path === "/api/health/details") {
      if (!state.health) return route.abort();
      json = state.health;
    } else if (path === "/api/team/session/exchange") {
      state.signedIn = true;
      json = {
        space_id: "space",
        space_kind: "team",
        user: { user_id: "human", display_name: "Researcher" },
      };
    } else if (path === "/api/identity" && !state.signedIn) {
      return route.fulfill({ status: 401, json: { detail: "sign in" } });
    } else if (path === "/api/identity")
      json = {
        space_id: "space",
        space_kind: "personal",
        user: { user_id: "human", display_name: "Researcher" },
      };
    else if (path === "/api/projects") json = [{ id: "demo", name: "Remote project", revision: 1 }];
    else if (path === "/api/projects/demo" || path.endsWith("/cached")) {
      if (state.connected) {
        state.reconnectLoads++;
        state.reconnectRequested.resolve();
      }
      json = project();
    } else if (path.endsWith("/readiness")) json = project();
    else if (path.endsWith("/revision")) {
      if (state.connected) state.reconnectObservations++;
      json = {
        revision: 1,
        snapshot_freshness: project().snapshot_freshness,
        last_remote_sync_at: project().last_remote_sync_at,
        graph_mutation: project().graph_mutation,
      };
    } else if (path.endsWith("/experiment-episodes") || path === "/api/episodes")
      json = { entries: [], unavailable: [] };
    else if (path.endsWith("/chats")) json = { chats: [], next_cursor: null };
    else if (path.endsWith("/chat-reads"))
      json = {
        baseline: "2026-01-01T00:00:00+00:00",
        reads: {},
        latest_finished: {},
        archived: [],
      };
    else if (path.endsWith("/usage")) json = { tasks: [], totals: {}, by_provider: [] };
    else if (path.endsWith("/digest"))
      json = {
        cursor: 0,
        mark: null,
        needs_you: [],
        changed: [],
        branches: [],
        ran: [],
        changed_node_ids: [],
        count: 0,
      };
    await route.fulfill({ json });
  });
  const readinessLoaded = page.waitForResponse((response) =>
    new URL(response.url()).pathname.endsWith("/readiness"),
  );
  await page.goto(`${origin}/#/projects/demo?view=overview`);
  return { page, errors, state, readinessLoaded };
}

for (const initialFreshness of ["stale", "fresh"]) {
  test(
    `Overview ignores legacy coverage and reconnect clears the offline banner from ${initialFreshness} cache`,
    { timeout: 15000 },
    async (t) => {
      const { page, errors, state, readinessLoaded } = await openDemoProject(t, initialFreshness);
      const banner = page.getByText("Canonical state is offline.", { exact: true });
      await banner.waitFor();
      await (await readinessLoaded).finished();
      await page.evaluate(() => new Promise(requestAnimationFrame));
      assert.equal(await page.getByText("Coverage boundary:").count(), 0);
      assert.equal(await page.getByText("Legacy agent-authored coverage claim.").count(), 0);
      state.connected = true;
      await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
      await state.reconnectRequested.promise;
      await banner.waitFor({ state: "detached" });
      assert.ok(
        state.reconnectObservations > 0,
        "heartbeat must observe the reconnection metadata",
      );
      assert.ok(
        state.reconnectLoads > 0,
        "heartbeat must fetch a project snapshot at the unchanged revision",
      );
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      await page.getByRole("button", { name: "Overview", exact: true }).click();
      assert.equal(await page.getByText("Coverage boundary:").count(), 0);
      assert.equal(await page.getByText("Legacy agent-authored coverage claim.").count(), 0);
      assert.deepEqual(errors, []);
    },
  );
}

// Drives the page's own identity module, as a failed request or poll would.
const reverify = (page) =>
  page.evaluate(async () => {
    const runtime = await import("/src/core/desktopRuntime.ts");
    await runtime.reverifyBackendIdentity("test");
  });

async function openedShell(t) {
  const opened = await openDemoProject(t, "fresh");
  await opened.page.locator(".app-shell").waitFor();
  await opened.readinessLoaded;
  const overview = await opened.page
    .getByRole("button", { name: "Overview", exact: true })
    .elementHandle();
  await opened.page.evaluate((button) => {
    // React reuses a same-type root element, so mark a node deep in the project.
    window.__projectNode = button;
    window.__sawLoadingScreen = false;
    new MutationObserver(() => {
      if (document.querySelector(".app-loading")) window.__sawLoadingScreen = true;
    }).observe(document.body, { childList: true, subtree: true });
  }, overview);
  return opened;
}

const shellStillMounted = (page) => page.evaluate(() => window.__projectNode.isConnected);

test(
  "a same-backend reverification keeps the project mounted under an overlay",
  {
    timeout: 20000,
  },
  async (t) => {
    const { page, errors, state } = await openedShell(t);
    state.health = null;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    assert.equal(await shellStillMounted(page), true);
    state.health = HEALTH;
    state.requests = [];
    await reverify(page);
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    await page.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith("/api/projects/demo"),
    );
    assert.equal(await shellStillMounted(page), true);
    assert.equal(await page.evaluate(() => window.__sawLoadingScreen), false);
    assert.equal(state.requests.filter((path) => path.endsWith("/cached")).length, 0);
    assert.deepEqual(
      errors.filter((message) => !message.includes("Failed to load resource")),
      [],
    );
  },
);

test("a changed backend clears the open project", { timeout: 20000 }, async (t) => {
  const { page, state } = await openedShell(t);
  state.health = { ...HEALTH, instance_id: "replacement" };
  await reverify(page);
  await page.locator(".reconnect-state").waitFor();
  assert.equal(await shellStillMounted(page), false);
  assert.equal(await page.getByRole("alertdialog").count(), 0);
});

test("a team sign-in keeps the pending project route", { timeout: 20000 }, async (t) => {
  const { page, errors } = await openDemoProject(t, "fresh", {
    health: { ...HEALTH, space_kind: "team" },
    signedIn: false,
  });
  await page.locator(".team-login-switch").click();
  await page.locator("#team-login-token").fill("member-token");
  await page.locator("#team-login-token").press("Enter");
  await page.getByRole("button", { name: "Overview", exact: true }).waitFor();
  assert.match(await page.evaluate(() => window.location.hash), /^#\/projects\/demo/);
  assert.deepEqual(
    errors.filter((message) => !message.includes("Failed to load resource")),
    [],
  );
});
