import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";
import { watermarkResponse } from "./watermarks.mjs";

let server;
let browser;
let origin;
before(async () => {
  server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    logLevel: "silent",
    configLoader: "runner",
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

const HEALTH = {
  status: "ok",
  space_id: "space",
  space_kind: "personal",
  instance_id: "test",
  version: "test-version",
  data_dir_id: "test-data",
};

// Serves one demo project; `state` steers health, connection, and request counts.
async function openDemoProject(t, initialFreshness, options = {}) {
  const context = await browser.newContext(options.viewport ? { viewport: options.viewport } : {});
  t.after(() => context.close());
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  const mainTarget = { kind: "main", branch_id: null };
  const providers = {};
  const profile = { provider: "codex", model: "", reasoning: "high", run_on: "local" };
  const state = {
    connected: false,
    health: options.health ?? HEALTH,
    signedIn: options.signedIn ?? true,
    member: "human",
    admitted: true,
    projectGate: null,
    projectStatus: 200,
    projectName: null,
    identityGate: null,
    requests: [],
    branchRequested: Promise.withResolvers(),
    reconnectLoads: 0,
    reconnectObservations: 0,
    reconnectRequested: Promise.withResolvers(),
    graphVersion: 0,
    graphRefs: options.graphRefs === undefined ? [] : options.graphRefs,
    branchGate: options.branchGate ?? null,
    chats: options.chats ?? [],
    episodes: options.episodes ?? [],
    watchers: options.watchers ?? [],
    mutations: [],
    renameStatus: 200,
    branchNames: {},
    epoch: "fixture",
    archivedChats: [],
    missingBranches: new Set(),
    taskRefusal: null,
    taskStarts: 0,
  };
  const project = (id = "demo", target = mainTarget) => ({
    id,
    name:
      state.branchNames[target.branch_id] ??
      state.projectName ??
      `Project for ${state.member} (${id})`,
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
    skill_defaults: { workflow_ids: [], skill_ids: [] },
    experiment_control: {},
    attention: {
      pending_proposal_ids: [],
      open_blocker_ids: [],
      decisions_awaiting_choice_ids: [],
      proposal_actions: {},
      stale_proposal_ids: [],
      refreshable_proposal_ids: [],
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
    const url = new URL(route.request().url());
    const path = url.pathname;
    const branchId = url.searchParams.get("branch_id");
    state.requests.push(branchId ? `${path}?branch_id=${branchId}` : path);
    const projectId = path.split("/")[3];
    let json = [];
    if (path === "/api/resource-watermarks") {
      if (state.connected) state.reconnectObservations++;
      // Reachability is part of the graph token, as on the server.
      json = {
        ...watermarkResponse(route.request().postDataJSON(), {
          version: (_id, resource) =>
            resource.name === "graph"
              ? `${state.connected}:${state.graphVersion}`
              : resource.name === "chats"
                ? `chats:${state.archivedChats.join(",")}`
                : "v1",
        }),
        epoch: state.epoch,
      };
    } else if (path === "/api/health" || path === "/api/health/details") {
      if (!state.health) return route.abort();
      json = state.health;
    } else if (path === "/api/team/session/exchange") {
      state.signedIn = true;
      json = {
        space_id: "space",
        space_kind: "team",
        user: { user_id: state.member, display_name: "Researcher" },
      };
    } else if (path === "/api/identity" && !state.signedIn) {
      return route.fulfill({ status: 401, json: { detail: "sign in" } });
    } else if (path === "/api/identity") {
      if (state.identityGate) await state.identityGate.promise;
      json = {
        space_id: "space",
        space_kind: state.health.space_kind,
        user: { user_id: state.member, display_name: "Researcher" },
      };
    } else if (/\/graph-refs\/[^/]+\/(name|archived)$/.test(path)) {
      const branch = state.graphRefs.find((ref) => ref.branch_id === path.split("/").at(-2));
      const body = route.request().postDataJSON();
      state.mutations.push({ path, body });
      if (path.endsWith("/name") && state.renameStatus !== 200)
        return route.fulfill({
          status: state.renameStatus,
          json: { detail: { code: "branch_name_taken" } },
        });
      Object.assign(branch, body);
      json = branch;
    } else if (path.endsWith("/graph-refs")) {
      if (state.graphRefs === null)
        return route.fulfill({ status: 503, json: { detail: "unavailable" } });
      json = state.graphRefs;
    } else if (path === "/api/projects")
      json = ["demo", "second"].map((id) => ({ id, name: project(id).name, revision: 1 }));
    else if (/^\/api\/projects\/[^/]+$/.test(path) || path.endsWith("/cached")) {
      if (state.connected) {
        state.reconnectLoads++;
        state.reconnectRequested.resolve();
      }
      if (state.projectStatus !== 200)
        return route.fulfill({ status: state.projectStatus, json: { detail: "unavailable" } });
      if (state.missingBranches.has(branchId))
        return route.fulfill({ status: 404, json: { detail: "Episode graph not found" } });
      const snapshot = project(
        projectId,
        branchId ? { kind: "branch", branch_id: branchId } : mainTarget,
      );
      if (state.projectGate) await state.projectGate.promise;
      if (branchId) state.branchRequested.resolve(branchId);
      if (branchId && state.branchGate) await state.branchGate.promise;
      if (!state.admitted) return route.fulfill({ status: 403, json: { detail: "denied" } });
      json = snapshot;
    } else if (path.endsWith("/readiness")) {
      const { providers, provider_readiness, provider_skill_inventories, agent_profiles } =
        project(projectId);
      json = { providers, provider_readiness, provider_skill_inventories, agent_profiles };
    } else if (path.endsWith("/revision")) {
      if (state.connected) state.reconnectObservations++;
      json = {
        revision: 1,
        snapshot_freshness: project().snapshot_freshness,
        last_remote_sync_at: project().last_remote_sync_at,
        graph_mutation: project().graph_mutation,
      };
    } else if (path === "/api/projects/demo/episodes") json = state.episodes;
    else if (path === "/api/projects/demo/watchers")
      json = state.watchers.filter(
        (watcher) => (watcher.graph_target.branch_id ?? null) === branchId,
      );
    else if (path.endsWith("/experiment-episodes") || path === "/api/episodes")
      json = { entries: [], unavailable: [] };
    else if (path.endsWith("/chats")) {
      const items = url.searchParams.has("inventory")
        ? state.chats
        : state.chats.filter((chat) => chat.graph_target.branch_id === branchId);
      json = { items, total: items.length, offset: 0, limit: 200 };
    } else if (path.endsWith("/chat-display"))
      json = { archived: state.archivedChats, titles: {}, pinned: [] };
    else if (/\/chats\/[^/]+\/archive$/.test(path)) {
      const chatId = path.split("/").at(-2);
      state.archivedChats = route.request().postDataJSON().archived
        ? [...new Set([...state.archivedChats, chatId])]
        : state.archivedChats.filter((id) => id !== chatId);
      json = { archived: state.archivedChats, titles: {}, pinned: [] };
    } else if (/\/chats\/[^/]+$/.test(path)) {
      const found = state.chats.find((chat) => chat.chat_id === path.split("/").at(-1));
      if (!found) return route.fulfill({ status: 404, json: { detail: "Chat not found" } });
      json = { ...found, messages: [] };
    } else if (path.endsWith("/tasks/project_chat")) {
      state.taskStarts++;
      if (state.taskRefusal) return route.fulfill({ status: 409, json: state.taskRefusal });
      return route.fulfill({ status: 500, json: { detail: "not modelled" } });
    } else if (path.endsWith("/chat-reads"))
      json = {
        baseline: "2026-01-01T00:00:00+00:00",
        reads: {},
        latest_finished: {},
        archived: state.archivedChats,
      };
    else if (path.endsWith("/usage")) json = { tasks: [], totals: {}, by_provider: [] };
    else if (path.endsWith("/digest"))
      json = {
        cursor: 0,
        mark: null,
        target: "main",
        needs_you: [],
        changed: [],
        branches: [],
        reports: [],
        changed_node_ids: [],
        count: 0,
      };
    await route.fulfill({ json });
  });
  const readinessLoaded = page.waitForResponse((response) =>
    new URL(response.url()).pathname.endsWith("/readiness"),
  );
  const authoritativeLoaded = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/projects/demo",
  );
  // A test that never reaches these reads must not fail when its page closes.
  readinessLoaded.catch(() => {});
  authoritativeLoaded.catch(() => {});
  if (options.desktop)
    await page.addInitScript((health) => {
      // The native shell, reduced to what the window's refresh path asks of it.
      window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener: () => {} };
      window.__TAURI_INTERNALS__ = {
        transformCallback: () => 1,
        invoke: async (command) =>
          command === "desktop_status" || command === "desktop_reconnect_backend"
            ? { ...health, base_url: location.origin }
            : null,
      };
    }, state.health);
  await page.goto(`${origin}/#/projects/demo?view=overview`);
  return { page, errors, state, readinessLoaded, authoritativeLoaded };
}

for (const initialFreshness of ["stale", "fresh"]) {
  test(
    `Overview ignores legacy coverage and reconnect clears the offline banner from ${initialFreshness} cache`,
    { timeout: 15000 },
    async (t) => {
      const { page, errors, state, readinessLoaded } = await openDemoProject(t, initialFreshness);
      const banner = page.locator(".state-offline");
      await banner.waitFor();
      await (await readinessLoaded).finished();
      await page.evaluate(() => new Promise(requestAnimationFrame));
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

async function openedShell(t, options) {
  const opened = await openDemoProject(t, "fresh", options);
  await opened.page.locator(".app-shell").waitFor();
  await opened.readinessLoaded;
  await (await opened.authoritativeLoaded).finished();
  await opened.page.evaluate(() => new Promise(requestAnimationFrame));
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
    // Healthy view navigation must never restart project opening.
    state.requests = [];
    await page.getByRole("button", { name: "Agents", exact: true }).click();
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    assert.equal(await shellStillMounted(page), true);
    assert.equal(state.requests.filter((path) => path.endsWith("/cached")).length, 0);
    state.health = null;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    assert.equal(await shellStillMounted(page), true);
    state.health = HEALTH;
    state.requests = [];
    state.identityGate = Promise.withResolvers();
    await reverify(page);
    assert.equal(await shellStillMounted(page), true);
    assert.equal(await page.getByRole("alertdialog").isVisible(), true);
    state.identityGate.resolve();
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    // The same backend's unchanged watermarks need no project reread.
    await page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/resource-watermarks",
    );
    assert.equal(state.requests.filter((path) => path === "/api/projects/demo").length, 0);
    assert.equal(await shellStillMounted(page), true);
    assert.equal(await page.evaluate(() => window.__sawLoadingScreen), false);
    assert.equal(state.requests.filter((path) => path.endsWith("/cached")).length, 0);
    assert.deepEqual(
      errors.filter((message) => !message.includes("Failed to load resource")),
      [],
    );
  },
);

for (const field of ["instance_id", "version", "data_dir_id"]) {
  test(
    `a changed backend ${field} keeps the held project during reconciliation`,
    { timeout: 20000 },
    async (t) => {
      const { page, state } = await openedShell(t);
      state.health = { ...HEALTH, [field]: "replacement" };
      state.admitted = false;
      state.identityGate = Promise.withResolvers();
      await reverify(page);
      await page.getByRole("alertdialog").waitFor();
      assert.equal(await shellStillMounted(page), true);
      const refused = page.waitForResponse(
        (response) => new URL(response.url()).pathname === "/api/projects/demo",
      );
      await page
        .getByRole("alertdialog")
        .getByRole("button", { name: "Reconnect", exact: true })
        .click();
      state.identityGate.resolve();
      await refused;
      await page.locator(".app-loading").waitFor({ state: "detached" });
      assert.equal(await shellStillMounted(page), true);
      assert.equal(await page.evaluate(() => window.__sawLoadingScreen), false);
    },
  );
}

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

async function signIn(page) {
  await page.locator(".team-login-switch").click();
  await page.locator("#team-login-token").fill("member-token");
  await page.locator("#team-login-token").press("Enter");
}

test(
  "reconnect blocks keyboard and pointer access until recovery",
  { timeout: 20000 },
  async (t) => {
    const { page, state } = await openedShell(t);
    const agents = page.getByRole("button", { name: "Agents", exact: true });
    await agents.focus();
    state.health = null;
    await reverify(page);
    const overlay = page.getByRole("alertdialog");
    await overlay.waitFor();
    assert.equal(
      await agents.evaluate((button) => {
        button.focus();
        return document.activeElement === button;
      }),
      false,
    );
    await page.keyboard.press("Escape");
    assert.equal(await overlay.isVisible(), true);
    assert.equal(
      await agents.click({ trial: true, timeout: 300 }).then(
        () => true,
        () => false,
      ),
      false,
    );
    state.health = HEALTH;
    await reverify(page);
    await overlay.waitFor({ state: "detached" });
    await agents.click();
    assert.equal(await shellStillMounted(page), true);
  },
);

for (const signInAgain of [true, false]) {
  test(
    `a different member clears all retained projects (${signInAgain ? "sign-in" : "identity read"})`,
    { timeout: 25000 },
    async (t) => {
      const { page, state } = await openedShell(t, { health: { ...HEALTH, space_kind: "team" } });
      // Both projects have cached snapshots and dock entries before identity changes.
      await page.evaluate(() => {
        window.location.hash = "/projects/second?view=overview";
      });
      await page
        .locator(".project-dock-tab.active .project-dock-select")
        .filter({ hasText: "Project for human (second)" })
        .waitFor();
      await page.evaluate(() => {
        window.location.hash = "/projects/demo?view=overview";
      });
      await page
        .locator(".project-dock-tab.active .project-dock-select")
        .filter({ hasText: "Project for human (demo)" })
        .waitFor();
      state.health = null;
      await reverify(page);
      await page.getByRole("alertdialog").waitFor();
      state.member = "other";
      state.admitted = false;
      state.projectGate = Promise.withResolvers();
      state.health = { ...HEALTH, space_kind: "team" };
      state.signedIn = !signInAgain;
      if (signInAgain) {
        await reverify(page);
        await page.locator(".team-login-switch").waitFor();
      }
      await page.evaluate(() => {
        window.__previousMemberPainted = false;
        new MutationObserver(() => {
          if (
            !document.querySelector(".reconnect-dialog") &&
            document.querySelector(".app-shell")?.textContent.includes("Project for human")
          ) {
            window.__previousMemberPainted = true;
          }
        }).observe(document.body, { childList: true, subtree: true, characterData: true });
      });
      const requested = page.waitForRequest(
        (request) => new URL(request.url()).pathname === "/api/projects/demo",
      );
      if (signInAgain) await signIn(page);
      else await reverify(page);
      await requested;
      assert.equal(await page.locator(".app-shell").count(), 0);
      state.projectGate.resolve();
      await page.locator(".app-loading").waitFor({ state: "detached" });
      assert.equal(await page.locator(".app-shell").count(), 0);
      assert.equal(await page.evaluate(() => window.__previousMemberPainted), false);
      assert.match(await page.evaluate(() => window.location.hash), /^#\/projects\/demo/);
      // Grant this member the second project; no old tab may paint while its
      // admitted snapshot is still pending, nor remain in the restored dock.
      state.admitted = true;
      state.projectGate = Promise.withResolvers();
      const secondRequested = page.waitForRequest(
        (request) => new URL(request.url()).pathname === "/api/projects/second",
      );
      await page.evaluate(() => {
        window.location.hash = "/projects/second?view=overview";
      });
      await secondRequested;
      assert.equal(await page.locator(".app-shell").count(), 0);
      state.projectGate.resolve();
      await page
        .locator(".project-dock-tab.active .project-dock-select")
        .filter({ hasText: "Project for other (second)" })
        .waitFor();
      assert.equal(await page.locator(".project-dock-tab").count(), 1);
      assert.equal(await page.evaluate(() => window.__previousMemberPainted), false);
    },
  );
}

test(
  "a refused reconciliation leaves the retained project usable and can recover again",
  { timeout: 20000 },
  async (t) => {
    const { page, state } = await openedShell(t);
    state.health = null;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    state.projectStatus = 503;
    state.health = HEALTH;
    // The project changed while the backend was out of reach.
    state.graphVersion += 1;
    const refused = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/projects/demo",
    );
    await reverify(page);
    await refused;
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    assert.equal(await shellStillMounted(page), true);
    assert.equal(await page.locator(".project-panel[inert]").count(), 0);
    state.health = null;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    state.projectStatus = 200;
    state.health = HEALTH;
    const recovered = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/projects/demo" && response.status() === 200,
    );
    await reverify(page);
    await recovered;
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    assert.equal(await shellStillMounted(page), true);
  },
);

test("a desktop refresh revalidates the shown project without holding it inert", async (t) => {
  const { page, state } = await openedShell(t, {
    desktop: true,
    health: { ...HEALTH, project_creation: { intents: [] } },
  });
  await page.evaluate(() => {
    window.__sawInert = false;
    new MutationObserver(() => {
      if (document.querySelector(".project-panel[inert]")) window.__sawInert = true;
    }).observe(document.body, { attributes: true, subtree: true, attributeFilter: ["inert"] });
  });
  const projectReads = () => state.requests.filter((path) => path === "/api/projects/demo").length;
  const readsBefore = projectReads();
  const revalidated = page.waitForRequest(
    (request) =>
      new URL(request.url()).pathname === "/api/resource-watermarks" &&
      request.postDataJSON().activated === "demo",
  );
  // A recovered team tunnel revalidates the window's project, as a Dock reopen does.
  await page.evaluate(
    (health) =>
      window.dispatchEvent(
        new CustomEvent("rcp:backend-identity", {
          detail: { ok: true, health, message: null, reason: "team-transport-recovered" },
        }),
      ),
    HEALTH,
  );
  await revalidated;
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert.equal(await page.evaluate(() => window.__sawInert), false);
  // Nothing changed, so nothing is downloaded again.
  assert.equal(projectReads(), readsBefore);
});

test("switching project tabs returns to the panel each project was left on", async (t) => {
  const { page, errors } = await openedShell(t);
  const current = (name) =>
    page.getByRole("button", { name, exact: true }).getAttribute("aria-current");
  const activeTab = (id) =>
    page
      .locator(".project-dock-tab.active .project-dock-select")
      .filter({ hasText: `(${id})` })
      .waitFor();
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  // A project opened without a remembered panel starts on Overview.
  await page.evaluate(() => {
    window.location.hash = "/projects/second";
  });
  await activeTab("second");
  assert.equal(await current("Overview"), "page");
  await page.locator(".project-dock-select").filter({ hasText: "(demo)" }).click();
  await activeTab("demo");
  assert.equal(await current("Agents"), "page");
  await page.locator(".project-dock-select").filter({ hasText: "(second)" }).click();
  await activeTab("second");
  assert.equal(await current("Overview"), "page");
  assert.deepEqual(
    errors.filter((message) => !message.includes("Failed to load resource")),
    [],
  );
});

test(
  "a tab keeps its page after a backend restart drops the retained tab data",
  { timeout: 20000 },
  async (t) => {
    const { page, state } = await openedShell(t);
    const current = (name) =>
      page.getByRole("button", { name, exact: true }).getAttribute("aria-current");
    const activeTab = (id) =>
      page
        .locator(".project-dock-tab.active .project-dock-select")
        .filter({ hasText: `(${id})` })
        .waitFor();
    await page.getByRole("button", { name: "Agents", exact: true }).click();
    await page.evaluate(() => {
      window.location.hash = "/projects/second";
    });
    await activeTab("second");
    state.health = { ...HEALTH, instance_id: "restarted" };
    await reverify(page);
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Reconnect", exact: true })
      .click();
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    await page.locator(".project-dock-select").filter({ hasText: "(demo)" }).click();
    await activeTab("demo");
    assert.equal(await current("Agents"), "page");
  },
);

test("browser reload restores the route through fresh admission", { timeout: 20000 }, async (t) => {
  const { page, state } = await openedShell(t);
  state.requests = [];
  state.admitted = false;
  const refused = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/projects/demo",
  );
  await page.reload();
  await refused;
  await page.locator(".app-loading").waitFor({ state: "detached" });
  assert.equal(await page.locator(".app-shell").count(), 0);
  assert.match(await page.evaluate(() => window.location.hash), /^#\/projects\/demo/);
  assert.ok(state.requests.includes("/api/identity"));
  assert.ok(state.requests.includes("/api/projects/demo"));
});

test(
  "a read interrupted by a backend change drops its late snapshot",
  { timeout: 20000 },
  async (t) => {
    const { page, state } = await openedShell(t);
    state.projectName = "Interrupted response";
    state.projectGate = Promise.withResolvers();
    const started = page.waitForRequest(
      (request) => new URL(request.url()).pathname === "/api/projects/demo",
    );
    state.graphVersion += 1;
    const request = await started;
    state.health = { ...HEALTH, instance_id: "replacement" };
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    state.projectGate.resolve();
    // The late read either lands or was aborted with its backend.
    await request.response();
    await page.evaluate(() => new Promise(requestAnimationFrame));
    assert.equal(
      await page
        .locator(".project-dock-select")
        .filter({ hasText: "Interrupted response" })
        .count(),
      0,
    );
    assert.equal(await shellStillMounted(page), true);
    state.projectName = "Current response";
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Reconnect", exact: true })
      .click();
    await page.locator(".project-dock-select").filter({ hasText: "Current response" }).waitFor();
    assert.equal(await shellStillMounted(page), true);
  },
);

const branchRef = (branchId) => ({
  kind: "branch",
  branch_id: branchId,
  episode_id: branchId,
  current_episode_id: branchId,
  base_head: { target: { kind: "main" }, revision: 1, transition_id: null },
  head: { target: { kind: "branch", branch_id: branchId }, revision: 1, transition_id: null },
  merge_eligible: false,
  merge_blocked_reason: null,
  merge_state: "unmerged",
  latest_successful_merge: null,
  active_merge_task_id: null,
  merge_diagnostic: null,
  archived: false,
  mode: "auto_research",
  name: branchId,
  running: false,
  needs_you: false,
  created_at: "2026-10-10T12:00:00Z",
});

const mainRef = () => ({
  ...branchRef("main"),
  kind: "main",
  branch_id: null,
  episode_id: null,
  current_episode_id: null,
  base_head: null,
  head: { target: { kind: "main" }, revision: 1, transition_id: null },
  merge_state: null,
});
const picker = (page) => page.locator(".branch-picker-trigger:visible");
const row = (page, id) => page.locator(`.branch-picker-row[data-branch-id="${id}"]`);
// The picker names the active ref once the hash route has applied.
const branchShown = (page, name) =>
  page.locator(`.branch-picker-trigger:visible[aria-label="Branch: ${name}"]`).waitFor();
async function switchBranch(page, name) {
  await picker(page).click();
  await page
    .getByRole("dialog", { name: "Branches", exact: true })
    .getByRole("button", { name, exact: true })
    .click();
}

const chat = {
  conversation_kind: "chat",
  graph_title: "main",
  orchestrator_episode_id: null,
  chat_id: "held-chat",
  graph_target: { kind: "main", branch_id: null },
  kind: "project_chat",
  node_id: null,
  title: "Loaded conversation",
  updated_at: "2026-10-10T12:00:00Z",
  message_count: 0,
  last_message_preview: "",
};

test("the Agents tab reopens the chat it was left on", async (t) => {
  const { page, errors } = await openedShell(t, { chats: [chat] });
  const agents = page.getByRole("button", { name: "Agents", exact: true });
  const chatMenu = page.getByRole("button", { name: "Chat menu", exact: true });
  const routeChat = () =>
    page.evaluate(() => new URLSearchParams(location.hash.split("?")[1]).get("chat"));
  await page.evaluate(() => (location.hash = "#/projects/demo?view=chats&chat=held-chat"));
  await chatMenu.waitFor();
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await chatMenu.waitFor({ state: "detached" });
  await agents.click();
  await chatMenu.waitFor();
  assert.equal(await routeChat(), "held-chat");
  // Inside Agents the tab still opens the board.
  await agents.click();
  await page.locator("[data-board-column]").first().waitFor();
  assert.equal(await routeChat(), null);
  assert.deepEqual(
    errors.filter((message) => !message.includes("Failed to load resource")),
    [],
  );
});

test(
  "following an origin link loads the source chat a bounded number of times and archive stays on its graph",
  { timeout: 25000 },
  async (t) => {
    const source = { ...chat, chat_id: "source", title: "Source chat", message_count: 2 };
    const fork = {
      ...source,
      chat_id: "fork",
      graph_target: { kind: "branch", branch_id: "b1" },
      // Newest first, so a selection leaving main's list would land on the fork.
      updated_at: "2026-10-11T12:00:00Z",
      origin: {
        source_chat_id: "source",
        source_operation_id: "turn",
        source_graph_target: { kind: "main", branch_id: null },
        source_title: "Source chat",
        provider: "codex",
        model: "",
        reasoning: "high",
        run_on: "local",
        pending: true,
      },
    };
    const { page, errors, state } = await openedShell(t, {
      graphRefs: [mainRef(), branchRef("b1")],
      chats: [fork, source],
    });
    const menu = page.getByRole("button", { name: "Chat menu", exact: true });
    await page.evaluate(() => (location.hash = "#/projects/demo?view=chats&chat=source"));
    await menu.waitFor();
    await page.evaluate(
      () => (location.hash = "#/projects/demo?view=chats&chat=fork&branch_id=b1"),
    );
    await branchShown(page, "b1");
    const before = state.requests.length;
    const sourceLoaded = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/projects/demo/chats/source",
    );
    await page.locator(".chat-origin-link").click();
    await sourceLoaded;
    await branchShown(page, "main");
    await menu.waitFor();
    // A refetch loop reissues the read as soon as its answer renders.
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    const sourceReads = state.requests
      .slice(before)
      .filter((path) => path === "/api/projects/demo/chats/source").length;
    assert.ok(sourceReads >= 1 && sourceReads <= 2, `source chat read ${sourceReads} times`);

    await menu.click();
    const archived = page.waitForResponse((response) =>
      response.url().endsWith("/chats/source/archive"),
    );
    await page.getByRole("menuitem", { name: "Archive", exact: true }).click();
    await archived;
    await page.evaluate(() => new Promise(requestAnimationFrame));
    assert.equal(
      await page.evaluate(() => new URLSearchParams(location.hash.split("?")[1]).has("branch_id")),
      false,
    );
    await branchShown(page, "main");
    assert.deepEqual(
      errors.filter((message) => !message.includes("Failed to load resource")),
      [],
    );
  },
);

test(
  "a switch with chats loaded survives identity re-check and fences late branch responses",
  { timeout: 30000 },
  async (t) => {
    const gate = Promise.withResolvers();
    const { page, errors, state } = await openedShell(t, {
      graphRefs: [mainRef(), branchRef("b1"), branchRef("b2")],
      branchGate: gate,
      chats: [chat],
    });
    await page.getByRole("button", { name: "Agents", exact: true }).click();
    await page.getByRole("button", { name: "Open Loaded conversation", exact: true }).waitFor();
    await switchBranch(page, "b1");
    await page.locator(".project-panel[inert]").waitFor();
    await page.getByRole("progressbar", { name: "Loading branch" }).waitFor();
    assert.equal(
      await page.evaluate(() => new URLSearchParams(location.hash.split("?")[1]).get("view")),
      "chats",
    );
    assert.equal(
      await page.evaluate(() => new URLSearchParams(location.hash.split("?")[1]).get("branch_id")),
      "b1",
    );
    assert.equal(await shellStillMounted(page), true);
    assert.equal(
      await page.getByRole("button", { name: "Agents", exact: true }).getAttribute("aria-current"),
      "page",
    );
    state.health = null;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    state.health = HEALTH;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    assert.equal(await shellStillMounted(page), true);
    assert.equal(await page.locator(".app-loading").count(), 0);
    state.branchGate = null;
    gate.resolve();
    await page.locator(".project-panel[inert]").waitFor({ state: "detached" });
    await branchShown(page, "b1");

    // Add an uncached branch after prefetch, then leave while its read is pending.
    state.graphRefs.push(branchRef("late"));
    state.branchGate = Promise.withResolvers();
    state.branchNames.late = "Late branch response";
    state.health = null;
    await reverify(page);
    state.health = HEALTH;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    const requested = page.waitForRequest(
      (request) => new URL(request.url()).searchParams.get("branch_id") === "late",
    );
    await switchBranch(page, "late");
    await requested;
    await switchBranch(page, "main");
    await page.locator(".project-panel[inert]").waitFor({ state: "detached" });
    const lateResponse = page.waitForResponse(
      (response) => new URL(response.url()).searchParams.get("branch_id") === "late",
    );
    state.branchGate.resolve();
    await (await lateResponse).finished();
    await page.evaluate(() => new Promise(requestAnimationFrame));
    assert.equal(
      await page
        .locator(".project-dock-select")
        .filter({ hasText: "Late branch response" })
        .count(),
      0,
    );
    assert.equal(
      await page.evaluate(() => new URLSearchParams(location.hash.split("?")[1]).has("branch_id")),
      false,
    );
    assert.equal(await shellStillMounted(page), true);
    assert.equal(await page.evaluate(() => window.__sawLoadingScreen), false);
    assert.deepEqual(
      errors.filter((message) => !message.includes("Failed to load resource")),
      [],
    );
  },
);

test(
  "picker orders live branches and exposes running, attention, and current markers",
  { timeout: 20000 },
  async (t) => {
    const { page, errors } = await openedShell(t, {
      graphRefs: [
        branchRef("older"),
        { ...branchRef("newer"), created_at: "2026-10-11T12:00:00Z", needs_you: true },
        { ...branchRef("running"), running: true },
        { ...branchRef("archived"), archived: true },
        mainRef(),
      ],
    });
    assert.equal(await page.locator(".branch-graph-banner").count(), 0);
    assert.equal(await picker(page).locator(".branch-picker-spinner").count(), 0);
    assert.equal(await picker(page).getByLabel("Another branch needs you").count(), 1);
    // Opening rereads the list, so markers follow task and question changes.
    const reread = page.waitForRequest((request) => request.url().endsWith("/graph-refs"));
    await picker(page).click();
    await reread;
    assert.deepEqual(
      await page
        .locator(".branch-picker-list .branch-picker-row")
        .evaluateAll((rows) => rows.map((row) => row.dataset.branchId)),
      ["main", "running", "newer", "older"],
    );
    assert.equal(await row(page, "main").getByLabel("Current branch").count(), 1);
    assert.equal(await row(page, "running").getByLabel("Running", { exact: true }).count(), 1);
    assert.equal(await row(page, "newer").getByLabel("Needs you", { exact: true }).count(), 1);
    assert.equal(await row(page, "main").getByRole("button").count(), 1);
    assert.equal(
      await row(page, "older").getByRole("button", { name: "Rename older" }).isVisible(),
      true,
    );
    await page.keyboard.press("Escape");
    assert.equal(await page.locator(".branch-picker-popover").count(), 0);
    await picker(page).click();
    await page.locator(".project-tabs").click({ position: { x: 1, y: 1 } });
    assert.equal(await page.locator(".branch-picker-popover").count(), 0);
    assert.deepEqual(errors, []);
  },
);

test(
  "rename validates inline and archive confirms only unmerged branches",
  { timeout: 25000 },
  async (t) => {
    const { page, state, errors } = await openedShell(t, {
      graphRefs: [mainRef(), branchRef("draft"), { ...branchRef("merged"), merge_state: "merged" }],
    });
    await picker(page).click();
    const originalHeight = (await row(page, "draft").boundingBox()).height;
    await page.getByRole("button", { name: "Rename draft", exact: true }).click();
    const input = page.getByRole("textbox", { name: "Branch name", exact: true });
    assert.equal((await row(page, "draft").boundingBox()).height, originalHeight);
    await input.fill("invalid_name");
    await input.press("Enter");
    await page.locator(".branch-picker-warning").waitFor();
    assert.equal(state.mutations.length, 0);
    state.renameStatus = 409;
    await input.fill("taken-name");
    await input.press("Enter");
    await page.locator(".branch-picker-warning").waitFor();
    assert.equal(state.mutations.length, 1);
    state.renameStatus = 200;
    await input.fill("renamed");
    // A saved rename rereads the list, so an older in-flight read cannot restore the old name.
    const reread = page.waitForRequest((request) => request.url().endsWith("/graph-refs"));
    await page.getByRole("button", { name: "Save branch name", exact: true }).click();
    await reread;
    await row(page, "draft").getByRole("button", { name: "renamed", exact: true }).waitFor();
    assert.deepEqual(state.mutations.at(-1).body, { name: "renamed" });
    await page.getByRole("button", { name: "Rename renamed", exact: true }).click();
    await input.fill("cancelled");
    await input.press("Escape");
    assert.equal(
      await row(page, "draft").getByRole("button", { name: "renamed", exact: true }).count(),
      1,
    );
    await page.getByRole("button", { name: "Archive renamed", exact: true }).click();
    const confirmation = page.locator(".branch-picker-confirm");
    await confirmation.waitFor();
    await confirmation.getByRole("button", { name: "Cancel", exact: true }).click();
    assert.equal(state.mutations.length, 2);
    await page.getByRole("button", { name: "Archive renamed", exact: true }).click();
    await confirmation.getByRole("button", { name: "Archive", exact: true }).click();
    await row(page, "draft").waitFor({ state: "detached" });
    assert.deepEqual(state.mutations.at(-1).body, { archived: true });
    await page.getByRole("button", { name: "merged", exact: true }).click();
    await page.locator(".project-panel[inert]").waitFor({ state: "detached" });
    await picker(page).click();
    await page.getByRole("button", { name: "Archive merged", exact: true }).click();
    await page.getByRole("button", { name: "Unarchive merged", exact: true }).waitFor();
    assert.equal(await confirmation.count(), 0);
    await page.getByRole("button", { name: "Unarchive merged", exact: true }).click();
    await page.getByRole("button", { name: "Archive merged", exact: true }).waitFor();
    assert.deepEqual(state.mutations.at(-1).body, { archived: false });
    assert.deepEqual(
      errors.filter((message) => !message.includes("Failed to load resource")),
      [],
    );
  },
);

test("a failed branch list keeps a disabled picker naming main", { timeout: 20000 }, async (t) => {
  const { page } = await openedShell(t, { graphRefs: null });
  await branchShown(page, "main");
  assert.equal(await picker(page).isDisabled(), true);
});

test(
  "a failed refresh keeps the branch name and disables the picker",
  { timeout: 20000 },
  async (t) => {
    const { page, state } = await openedShell(t, {
      graphRefs: [mainRef(), branchRef("kept")],
    });
    await switchBranch(page, "kept");
    await branchShown(page, "kept");
    state.graphRefs = null;
    // Opening rereads the list; the failed read closes it and keeps the name.
    await picker(page).click();
    await page.locator(".branch-picker-trigger:visible[disabled]").waitFor();
    await branchShown(page, "kept");
    assert.equal(await page.locator(".branch-picker-popover").count(), 0);
  },
);

test(
  "phone chip opens the shared picker and stays within the viewport",
  { timeout: 20000 },
  async (t) => {
    const { page } = await openedShell(t, {
      viewport: { width: 390, height: 844 },
      graphRefs: [mainRef(), { ...branchRef("phone"), needs_you: true }],
    });
    assert.equal(
      await page.locator(".branch-picker-compact .branch-picker-trigger").isVisible(),
      true,
    );
    await picker(page).click();
    const bounds = await page.locator(".branch-picker-popover").boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
    await page
      .getByRole("dialog", { name: "Branches", exact: true })
      .getByRole("button", { name: "phone", exact: true })
      .click();
    await page.locator(".project-panel[inert]").waitFor({ state: "detached" });
    await branchShown(page, "phone");
    assert.equal(await picker(page).getByLabel("Another branch needs you").count(), 0);
  },
);

test(
  "unvisited branches are never warmed and a revisited branch paints at once",
  { timeout: 25000 },
  async (t) => {
    const { page, state } = await openedShell(t, {
      graphRefs: [
        mainRef(),
        branchRef("first"),
        branchRef("second"),
        { ...branchRef("archived"), archived: true },
      ],
    });
    // Let a few beats pass; none may read an unvisited branch.
    for (let beat = 0; beat < 2; beat++)
      await page.waitForResponse(
        (response) => new URL(response.url()).pathname === "/api/resource-watermarks",
      );
    assert.equal(
      state.requests.some((path) => path.includes("branch_id=")),
      false,
    );
    await switchBranch(page, "second");
    await page.locator(".project-panel[inert]").waitFor({ state: "detached" });
    await branchShown(page, "second");
    await switchBranch(page, "main");
    await branchShown(page, "main");
    state.branchGate = Promise.withResolvers();
    await switchBranch(page, "second");
    assert.equal(await page.locator(".project-panel[inert]").count(), 0);
    assert.equal(await page.evaluate(() => window.__sawLoadingScreen), false);
    assert.equal(
      state.requests.some((path) => /branch_id=(first|archived)/.test(path)),
      false,
    );
    state.branchGate.resolve();
  },
);

const hashParams = (page) =>
  page.evaluate(() => Object.fromEntries(new URLSearchParams(location.hash.split("?")[1])));
const composer = (page) => page.getByRole("textbox", { name: "Message", exact: true });

test(
  "a send refused by a replaced backend keeps its text and Reconnect keeps the open chat",
  { timeout: 25000 },
  async (t) => {
    const { page, state } = await openedShell(t, { chats: [chat] });
    await page.evaluate(() => (location.hash = "#/projects/demo?view=chats&chat=held-chat"));
    await composer(page).fill("keep this text");
    state.health = { ...HEALTH, instance_id: "replacement" };
    state.taskRefusal = {
      detail: { code: "backend_instance_replaced", message: "replaced" },
      instance_id: "replacement",
    };
    await composer(page).press("Enter");
    await page.getByRole("alertdialog").waitFor();
    assert.equal(state.taskStarts, 1);
    await page
      .getByRole("alertdialog")
      .getByRole("button", { name: "Reconnect", exact: true })
      .click();
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    await composer(page).waitFor();
    assert.equal(await composer(page).inputValue(), "keep this text");
    assert.equal((await hashParams(page)).chat, "held-chat");
    // The refused send left no failed turn to resend.
    assert.equal(await page.locator(".node-chat-line.unsent").count(), 0);
  },
);

test("a restarted backend is offered Reconnect at the next watermark beat", async (t) => {
  const { page, state } = await openedShell(t);
  state.health = { ...HEALTH, instance_id: "replacement" };
  state.epoch = "restarted";
  await page.getByRole("alertdialog").waitFor();
  assert.equal(await shellStillMounted(page), true);
});

test(
  "an archive from another tab closes this tab's composer at its next chats refresh",
  { timeout: 20000 },
  async (t) => {
    const { page, state } = await openedShell(t, { chats: [chat] });
    await page.evaluate(() => (location.hash = "#/projects/demo?view=chats&chat=held-chat"));
    await composer(page).waitFor();
    state.archivedChats = ["held-chat"];
    const restore = page
      .locator(".chat-dock")
      .getByRole("button", { name: "Restore", exact: true });
    await restore.waitFor();
    assert.equal(await composer(page).count(), 0);
    await restore.click();
    await composer(page).waitFor();
    assert.deepEqual(state.archivedChats, []);
  },
);

test(
  "a chat link opens the chat on its own graph whatever branch_id it names",
  { timeout: 25000 },
  async (t) => {
    const branchChat = {
      ...chat,
      chat_id: "branch-chat",
      title: "Branch conversation",
      graph_target: { kind: "branch", branch_id: "b1" },
    };
    const { page } = await openedShell(t, {
      graphRefs: [mainRef(), branchRef("b1"), branchRef("b2")],
      chats: [chat, branchChat],
    });
    for (const link of [
      "#/projects/demo?view=chats&chat=branch-chat&branch_id=b2",
      "#/projects/demo?view=chats&chat=held-chat",
      "#/projects/demo?view=chats&chat=branch-chat",
    ]) {
      await page.evaluate((hash) => (location.hash = hash), link);
      const chatId = new URLSearchParams(link.split("?")[1]).get("chat");
      await branchShown(page, chatId === "branch-chat" ? "b1" : "main");
      await composer(page).waitFor();
      assert.equal((await hashParams(page)).chat, chatId);
    }
    // A chat no graph lists selects nothing, not the chat open before.
    await page.evaluate(
      () => (location.hash = "#/projects/demo?view=chats&chat=ghost&branch_id=b1"),
    );
    await page.locator(".toast.error").waitFor();
    assert.equal(await composer(page).count(), 0);
    assert.equal((await hashParams(page)).chat, undefined);
  },
);

test(
  "a main link to a branch orchestrator opens it on main with its watchers",
  { timeout: 25000 },
  async (t) => {
    const orchestrator = {
      ...chat,
      chat_id: "b1",
      title: "Branch orchestrator",
      conversation_kind: "auto_research",
      orchestrator_episode_id: "b1",
      graph_target: { kind: "branch", branch_id: "b1" },
    };
    const episode = {
      episode_id: "b1",
      mode: "auto_research",
      status: "running",
      graph_target: orchestrator.graph_target,
      graph_branch: branchRef("b1"),
      tasks: [],
      chain: [],
    };
    const watcher = {
      watcher_id: "branch-watcher",
      chat_id: "b1",
      status: "active",
      graph_target: orchestrator.graph_target,
      continuation: { patch_kind: "project_chat" },
    };
    const { page } = await openedShell(t, {
      graphRefs: [mainRef(), branchRef("b1")],
      chats: [orchestrator],
      episodes: [episode],
      watchers: [watcher],
    });
    await page.evaluate(() => (location.hash = "#/projects/demo?view=chats&chat=b1"));
    await branchShown(page, "main");
    await page.locator(".chat-dock").getByText("Branch orchestrator").first().waitFor();
    assert.equal((await hashParams(page)).chat, "b1");
    // Its branch's watchers show although main's watcher list has none.
    await page.locator(".chat-watcher-count").waitFor();
    assert.equal(await page.locator(".toast.error").count(), 0);
  },
);

test("an empty or unknown branch_id opens main instead of a blank target", async (t) => {
  const { page, state } = await openedShell(t, { graphRefs: [mainRef(), branchRef("b1")] });
  state.missingBranches.add("gone");
  for (const branch of ["gone", ""]) {
    await page.evaluate(
      (id) => (location.hash = `#/projects/demo?view=overview&branch_id=${id}`),
      branch,
    );
    await page.waitForFunction(() => !location.hash.includes("branch_id"));
    await branchShown(page, "main");
    await page.locator(".toast.error").waitFor();
  }
});
