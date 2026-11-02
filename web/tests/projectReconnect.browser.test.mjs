import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { chromium } from "playwright";
import { createServer } from "vite";
import { watermarkResponse } from "./watermarks.mjs";
import { episode as baseEpisode } from "./fixtures/campaigns.mjs";
import { timelineFixture } from "./fixtures/timeline.mjs";

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
    connected: options.connected ?? false,
    health: options.health ?? HEALTH,
    signedIn: options.signedIn ?? true,
    member: "human",
    admitted: true,
    projectGate: null,
    indexGate: null,
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
    taskListStatus: 200,
    tasks: [],
    watermarkGate: null,
    resourceVersions: {},
    payloadGates: new Map(),
    transcripts: options.transcripts ?? {},
    artifacts: [],
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
      stale_edge_ids: [],
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
                : (state.resourceVersions[resource.name] ?? "v1"),
        }),
        epoch: state.epoch,
      };
      if (state.watermarkGate) await state.watermarkGate.promise;
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
    } else if (path === "/api/projects") {
      if (state.indexGate) await state.indexGate.promise;
      json = ["demo", "second"].map((id) => ({ id, name: project(id).name, revision: 1 }));
    } else if (/^\/api\/projects\/[^/]+$/.test(path) || path.endsWith("/cached")) {
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
    else if (/\/episodes\/[^/]+\/timeline$/.test(path))
      json = timelineFixture(path.split("/").at(-2));
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
      json = { ...found, messages: state.transcripts[found.chat_id] ?? [] };
    } else if (path.endsWith("/tasks") && state.taskListStatus !== 200) {
      return route.fulfill({
        status: state.taskListStatus,
        json: { detail: "Task list unavailable" },
      });
    } else if (path.endsWith("/tasks")) json = state.tasks;
    else if (/\/tasks\/[^/]+$/.test(path) && route.request().method() === "GET")
      json = state.tasks.find((task) => task.operation_id === path.split("/").at(-1)) ?? null;
    else if (path.endsWith("/tasks/project_chat")) {
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
    else if (path.endsWith("/artifacts")) json = state.artifacts;
    else if (path.endsWith("/digest") && options.digest) {
      await options.digest.gate;
      json = options.digest.value;
    } else if (path.endsWith("/digest"))
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
    if (state.payloadGates.has(path)) await state.payloadGates.get(path).promise;
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
  if (options.initScript) await page.addInitScript(options.initScript);
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

test("Overview rows first show where they stay when the digest arrives late", async (t) => {
  const digest = Promise.withResolvers();
  const { page, errors } = await openDemoProject(t, "fresh", {
    digest: {
      gate: digest.promise,
      value: {
        cursor: 2,
        mark: null,
        target: "main",
        needs_you: [],
        changed: [{ source_key: "k", label: "Changed node", edits: 1, node_ids: [] }],
        reports: [],
        changed_node_ids: [],
        count: 1,
      },
    },
    initScript: () => {
      // Every distinct top at which the rows were visible, frame by frame.
      window.__rowTops = [];
      const tick = () => {
        const rows = document.querySelector(".overview-questions");
        if (rows?.checkVisibility({ visibilityProperty: true })) {
          const top = Math.round(rows.getBoundingClientRect().top);
          if (window.__rowTops.at(-1) !== top) window.__rowTops.push(top);
        }
        requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    },
  });
  await page.locator(".overview-questions").waitFor({ state: "attached" });
  await page.evaluate(() => new Promise(requestAnimationFrame));
  digest.resolve();
  await page.locator(".digest-card").waitFor();
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert.equal((await page.evaluate(() => window.__rowTops)).length, 1);
  assert.deepEqual(errors, []);
});

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
  "a refused reconciliation withholds retained content and its visible retry recovers",
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
    const retry = page.getByRole("button", { name: "Retry loading project", exact: true });
    await retry.waitFor();
    assert.equal(await page.locator('.project-panel[aria-busy="true"]').count(), 1);
    assert.equal(await page.locator(".project-panel .overview-questions").count(), 0);
    state.projectStatus = 200;
    const recovered = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/projects/demo" && response.status() === 200,
    );
    await retry.click();
    await recovered;
    await retry.waitFor({ state: "detached" });
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
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

test("a loaded index stays on screen while a quick return refreshes it", async (t) => {
  const useCheckout = {
    intent: "use_existing_checkout_personally",
    eligible: true,
    preselected: true,
    primary_action_label: "Use existing checkout",
    required_fields: [],
    pinned_source_project_id: null,
    unavailable_reason: null,
  };
  const { page, state, authoritativeLoaded } = await openDemoProject(t, "fresh", {
    health: { ...HEALTH, project_creation: { intents: [useCheckout] } },
  });
  await authoritativeLoaded;
  await page.evaluate(() => {
    location.hash = "#/cards";
  });
  await page.locator(".project-shelf").waitFor();
  // Leave for a project whose snapshot is still loading, then come straight back.
  state.projectGate = Promise.withResolvers();
  await page.evaluate(() => {
    location.hash = "#/projects/second";
  });
  await page.locator(".app-loading").waitFor();
  state.indexGate = Promise.withResolvers();
  await page.evaluate(() => {
    location.hash = "#/cards";
  });
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert.equal(await page.locator(".project-shelf").count(), 1);
  assert.equal(await page.locator(".app-loading").count(), 0);
  state.indexGate.resolve();
  state.projectGate.resolve();
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
    await page.locator(".chat-origin-link").click();
    await branchShown(page, "main");
    await menu.waitFor();
    // A refetch loop reissues the read as soon as its answer renders.
    await page.evaluate(
      () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
    );
    const sourceReads = state.requests
      .slice(before)
      .filter((path) => path === "/api/projects/demo/chats/source").length;
    // A fresh matching watermark may admit the retained transcript without a read.
    assert.ok(sourceReads <= 2, `source chat read ${sourceReads} times`);

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
    await page.locator('.project-panel[aria-busy="true"]').waitFor();
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
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
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
    const lateRequest = await requested;
    await switchBranch(page, "main");
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
    state.branchGate.resolve();
    // Leaving the target may cancel the read entirely. If it survives, its
    // response is still fenced from the currently presented graph.
    const lateResponse = await lateRequest.response();
    if (lateResponse) await lateResponse.finished();
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
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
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
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
    await branchShown(page, "phone");
    assert.equal(await picker(page).getByLabel("Another branch needs you").count(), 0);
  },
);

test(
  "unvisited branches stay cold and a revisited branch reuses cache after a fresh observation",
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
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
    await branchShown(page, "second");
    await switchBranch(page, "main");
    await branchShown(page, "main");
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
    state.watermarkGate = Promise.withResolvers();
    const before = state.requests.filter(
      (path) => path === "/api/projects/demo?branch_id=second",
    ).length;
    await switchBranch(page, "second");
    await page.locator('.project-panel[aria-busy="true"]').waitFor();
    assert.equal(await page.locator(".project-panel .overview-questions").count(), 0);
    state.watermarkGate.resolve();
    state.watermarkGate = null;
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
    assert.equal(
      state.requests.filter((path) => path === "/api/projects/demo?branch_id=second").length,
      before,
    );
    assert.equal(await page.evaluate(() => window.__sawLoadingScreen), false);
    assert.equal(
      state.requests.some((path) => /branch_id=(first|archived)/.test(path)),
      false,
    );
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

const transcriptMessage = (text) => ({
  message_id: text,
  operation_id: "completed-turn",
  role: "assistant",
  text,
  timestamp: "2026-10-10T12:00:00Z",
  native_session_id: null,
  provider: "codex",
  model: null,
  reasoning: null,
  execution_machine: "local",
  applied_revision: null,
  mode: "discuss",
  graph_update: null,
  trigger: "human",
  attachments: [],
});

// Capture DOM mutations as well as frames: a layout-effect correction cannot
// conceal an old entry that React already committed in the returning panel.
async function recordReturningContent(page, selector, text) {
  await page.evaluate(
    ({ selector, text }) => {
      window.__staleEntryFrames = [];
      const sample = () => {
        const panel = document.querySelector(selector);
        if (panel?.textContent.includes(text)) window.__staleEntryFrames.push(panel.textContent);
      };
      window.__entryObserver?.disconnect();
      window.__entryObserver = new MutationObserver(sample);
      window.__entryObserver.observe(document.body, {
        childList: true,
        subtree: true,
        characterData: true,
      });
      window.__sampleEntryFrame = sample;
    },
    { selector, text },
  );
}

const sampleEntryFrames = (page) =>
  page.evaluate(async () => {
    await new Promise(requestAnimationFrame);
    window.__sampleEntryFrame();
    await new Promise(requestAnimationFrame);
    window.__sampleEntryFrame();
    return window.__staleEntryFrames;
  });

test("returning Agents admits the current transcript before its first frame at 390px", async (t) => {
  const summary = { ...chat, message_count: 1 };
  const { page, errors, state } = await openedShell(t, {
    viewport: { width: 390, height: 844 },
    chats: [summary],
    transcripts: { "held-chat": [transcriptMessage("Retained transcript")] },
  });
  await page.evaluate(() => (location.hash = "#/projects/demo?view=chats&chat=held-chat"));
  await page.getByText("Retained transcript", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await page.locator(".node-chat-lines").waitFor({ state: "detached" });
  const gate = Promise.withResolvers();
  state.watermarkGate = gate;
  // This request started while Agents was still dormant. Its late reply must
  // not validate the new entry, even though its version matches retained data.
  const oldWatermark = page.waitForRequest(
    (request) => new URL(request.url()).pathname === "/api/resource-watermarks",
  );
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await oldWatermark;
  state.resourceVersions.chat = "updated-chat";
  state.transcripts["held-chat"] = [transcriptMessage("Current transcript")];
  const payload = Promise.withResolvers();
  state.payloadGates.set("/api/projects/demo/chats/held-chat", payload);
  await recordReturningContent(page, ".node-chat-lines", "Retained transcript");
  await page.locator(".phone-tab-bar").getByRole("button", { name: /^More/ }).click();
  await page.getByRole("button", { name: "Agents", exact: true }).click();
  assert.deepEqual(await sampleEntryFrames(page), []);
  const transcriptRequested = page.waitForRequest(
    (request) => new URL(request.url()).pathname === "/api/projects/demo/chats/held-chat",
  );
  gate.resolve();
  state.watermarkGate = null;
  await transcriptRequested;
  assert.deepEqual(await sampleEntryFrames(page), []);
  payload.resolve();
  state.payloadGates.clear();
  await page.getByText("Current transcript", { exact: true }).waitFor();
  assert.deepEqual(await sampleEntryFrames(page), []);
  const editor = page.getByRole("textbox", { name: "Message", exact: true });
  await editor.fill("Unsent draft");
  await editor.focus();
  const editorElement = await editor.elementHandle();
  const pollingPayload = Promise.withResolvers();
  state.payloadGates.set("/api/projects/demo/chats/held-chat", pollingPayload);
  state.resourceVersions.chat = "next-chat";
  state.transcripts["held-chat"] = [transcriptMessage("Polled transcript")];
  const pollRequested = page.waitForRequest(
    (request) => new URL(request.url()).pathname === "/api/projects/demo/chats/held-chat",
  );
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await pollRequested;
  assert.equal(await page.getByText("Current transcript", { exact: true }).isVisible(), true);
  assert.equal(await editor.inputValue(), "Unsent draft");
  assert.equal(await editorElement.evaluate((element) => document.activeElement === element), true);
  pollingPayload.resolve();
  state.payloadGates.clear();
  await page.getByText("Polled transcript", { exact: true }).waitFor();
  assert.equal(await editor.inputValue(), "Unsent draft");
  assert.equal(await editorElement.evaluate((element) => document.activeElement === element), true);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(
    errors.filter((message) => !message.includes("Failed to load resource")),
    [],
  );
});

test("returning Artifacts withholds its retained rows until current admission at 390px", async (t) => {
  const { page, errors, state } = await openedShell(t, { viewport: { width: 390, height: 844 } });
  const artifact = (name) => ({
    id: name,
    artifact_id: name,
    name,
    available: true,
    can_open: false,
    can_download: false,
    view: "image",
  });
  state.artifacts = [artifact("Retained artifact")];
  await page.locator(".phone-tab-bar").getByRole("button", { name: /^More/ }).click();
  await page
    .locator(".phone-tab-bar")
    .getByRole("button", { name: /^Artifacts/ })
    .click();
  await page.getByRole("heading", { name: "Retained artifact" }).waitFor();
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  state.watermarkGate = Promise.withResolvers();
  state.resourceVersions.artifacts = "updated-artifacts";
  state.artifacts = [artifact("Current artifact")];
  const payload = Promise.withResolvers();
  state.payloadGates.set("/api/projects/demo/artifacts", payload);
  await recordReturningContent(page, ".artifacts-view", "Retained artifact");
  await page.locator(".phone-tab-bar").getByRole("button", { name: /^More/ }).click();
  await page
    .locator(".phone-tab-bar")
    .getByRole("button", { name: /^Artifacts/ })
    .click();
  assert.deepEqual(await sampleEntryFrames(page), []);
  const artifactsRequested = page.waitForRequest(
    (request) => new URL(request.url()).pathname === "/api/projects/demo/artifacts",
  );
  state.watermarkGate.resolve();
  state.watermarkGate = null;
  await artifactsRequested;
  assert.deepEqual(await sampleEntryFrames(page), []);
  payload.resolve();
  state.payloadGates.clear();
  await page.getByRole("heading", { name: "Current artifact" }).waitFor();
  assert.deepEqual(await sampleEntryFrames(page), []);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.deepEqual(
    errors.filter((message) => !message.includes("Failed to load resource")),
    [],
  );
});

test("a current task-list failure remains visible and its retry recovers", async (t) => {
  const { page, state } = await openedShell(t);
  state.taskListStatus = 503;
  state.resourceVersions["tasks.status"] = "tasks-changed";
  const failed = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/projects/demo/tasks" && response.status() === 503,
  );
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await failed;
  const retry = page.getByRole("button", { name: "Retry tasks", exact: true });
  await retry.waitFor();
  state.taskListStatus = 200;
  const recovered = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/api/projects/demo/tasks" && response.status() === 200,
  );
  await retry.click();
  await recovered;
  await retry.waitFor({ state: "detached" });
});

function mergeTask(operationId, branchId = "episode-branch") {
  return {
    operation_id: operationId,
    project_id: "demo",
    kind: "branch_merge",
    status: "running",
    request: { provider: "codex" },
    graph_target: { kind: "branch", branch_id: branchId },
    created_at: "2026-10-10T12:00:00Z",
    updated_at: "2026-10-10T12:00:01Z",
    status_message: "Merging branch",
    status_label: "Running",
    phase: "running",
    runtime_id: "codex",
    runtime_label: "Codex",
    provider_label: "Codex",
    attempt: 1,
    history_only: false,
    elapsed_seconds: 1,
    progress: 0,
    estimate_seconds: 10,
    estimate_samples: 1,
    active: true,
    queued: false,
    pausing: false,
    awaiting_human: false,
    paused: false,
    failed: false,
    settled: false,
    finished: false,
    can_pause: false,
    can_resume: false,
    can_retry: false,
    steer_visible: false,
    can_steer: false,
    steer_unavailable_reason: null,
    steer_turn_id: null,
    steer_action_label: "",
    events: [],
  };
}

function mergeFixture(episodeOwned = true) {
  const ref = {
    ...branchRef(episodeOwned ? "episode-branch" : "member-fork"),
    parent_head: mainRef().head,
    episode_id: episodeOwned ? "chain-root" : null,
    current_episode_id: episodeOwned ? "chain-current" : null,
    mode: episodeOwned ? "auto_research" : null,
    merge_eligible: true,
  };
  const episode = {
    ...baseEpisode,
    project_id: "demo",
    episode_id: "chain-current",
    tasks: [],
    chain: [],
    status: "completed",
    health: "needs_action",
    run_section: "actionable",
    recommendation: "review",
    live: false,
    can_stop: false,
    can_merge: true,
    can_message: false,
    task_control: null,
    root_operation_id: null,
    current_operation_id: null,
    current_orchestrator_task_id: null,
    current_control_task_id: null,
    graph_target: ref.head.target,
    graph_base_head: ref.base_head,
    graph_branch: ref,
    graph_isolation: true,
    code_worktree: false,
    isolation_owner_episode_id: "chain-root",
    isolation_state: null,
  };
  return { ref, episode };
}

async function submitPickerMerge(page, ref) {
  await picker(page).click();
  await page.getByRole("button", { name: `Merge ${ref.name}`, exact: true }).click();
  await page.locator("dialog").getByRole("button", { name: "Merge", exact: true }).click();
}

for (const owned of [true, false]) {
  test(`App dispatches ${owned ? "episode" : "member fork"} picker merge and keeps 390px navigation usable`, async (t) => {
    const { ref, episode } = mergeFixture(owned);
    const { page, state, errors } = await openedShell(t, {
      connected: true,
      viewport: { width: 390, height: 844 },
      graphRefs: [mainRef(), ref],
      episodes: owned ? [episode] : [],
    });
    const submitted = Promise.withResolvers();
    const response = Promise.withResolvers();
    const requests = [];
    const task = mergeTask(`merge-${ref.branch_id}`, ref.branch_id);
    await page.route(
      /\/api\/projects\/demo\/(episodes|graph-refs)\/[^/]+\/merge$/,
      async (route) => {
        const request = {
          path: new URL(route.request().url()).pathname,
          body: route.request().postDataJSON(),
        };
        requests.push(request);
        if (owned && !request.body.confirm_unfinished_jobs)
          return route.fulfill({
            status: 409,
            json: {
              detail: {
                code: "unfinished_jobs_confirmation_required",
                jobs: [
                  {
                    kind: "watcher",
                    id: "training-job",
                    command: "python train.py",
                    status: "unobservable",
                  },
                ],
              },
            },
          });
        submitted.resolve(request);
        await response.promise;
        state.tasks = [task];
        await route.fulfill({
          json: owned
            ? { ...episode, accepted_merge_task: task }
            : { operation_id: task.operation_id, status: "running" },
        });
      },
    );
    await submitPickerMerge(page, ref);
    if (owned) {
      await page.getByRole("list", { name: "Unfinished jobs", exact: true }).waitFor();
      await page.getByRole("button", { name: "Merge anyway", exact: true }).click();
    }
    const request = await submitted.promise;
    assert.equal(
      request.path,
      owned
        ? "/api/projects/demo/episodes/chain-current/merge"
        : "/api/projects/demo/graph-refs/member-fork/merge",
    );
    assert.match(request.body.request_id, /^[0-9a-f-]{36}$/);
    if (owned)
      assert.deepEqual(request.body, {
        request_id: request.body.request_id,
        history_mode: "merge",
        keep_branch_open: true,
        archive_graph_branch: false,
        remove_worktree: false,
        delete_code_branch: false,
        confirm_unfinished_jobs: true,
      });
    else assert.deepEqual(request.body, { request_id: request.body.request_id });
    assert.equal(await page.locator("dialog").count(), 0);
    await page.getByRole("button", { name: "Inbox", exact: true }).click();
    assert.equal(
      await page.getByRole("button", { name: "Inbox", exact: true }).getAttribute("aria-current"),
      "page",
    );
    await page.locator('.project-panel[aria-busy="false"]').waitFor();
    response.resolve();
    await page.locator(".toast").filter({ hasText: "Merge is running" }).waitFor();
    await page.getByRole("button", { name: "Project menu", exact: true }).click();
    await page
      .getByRole("button", { name: "Project history, task in progress", exact: true })
      .waitFor();
    assert.equal(await page.locator(".activity-pulse:visible").count(), 1);
    assert.equal(requests.length, owned ? 2 : 1);
    if (owned) assert.equal(requests[0].body.confirm_unfinished_jobs, undefined);
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    assert.deepEqual(
      errors.filter((message) => !message.includes("409 (Conflict)")),
      [],
    );
  });
}

for (const owned of [true, false]) {
  test(`artifact pin picker dispatches ${owned ? "episode" : "fork"} merge for its own project from landing`, async (t) => {
    const { ref, episode: sourceEpisode } = mergeFixture(owned);
    const { page, state, errors } = await openedShell(t, {
      connected: true,
      viewport: { width: 390, height: 844 },
      health: {
        ...HEALTH,
        project_creation: {
          intents: [
            {
              intent: "use_existing_checkout_personally",
              eligible: true,
              preselected: true,
              primary_action_label: "Use existing checkout",
              required_fields: [],
            },
          ],
        },
      },
    });
    const projectId = "second";
    const episode = { ...sourceEpisode, project_id: projectId };
    const task = { ...mergeTask(`artifact-${owned}`, ref.branch_id), project_id: projectId };
    const submitted = Promise.withResolvers();
    const response = Promise.withResolvers();
    const requests = [];
    await page.route("**/api/projects/second/graph-refs", (route) =>
      route.fulfill({ json: [mainRef(), ref] }),
    );
    await page.route("**/api/projects/second/artifacts/preview/state", (route) =>
      route.fulfill({
        json: {
          artifact_id: "preview",
          name: "Merge preview artifact",
          pinned_branch_ids: [],
          pin_branch_id: null,
          can_pin: true,
          version_number: 1,
          viewer_url: "/api/projects/second/artifacts/preview/viewer",
        },
      }),
    );
    await page.route("**/api/projects/second/artifacts/preview/viewer", (route) =>
      route.fulfill({ contentType: "text/html", body: "<p>Preview</p>" }),
    );
    await page.route(
      /\/api\/projects\/second\/(episodes|graph-refs)\/[^/]+\/merge$/,
      async (route) => {
        const request = {
          path: new URL(route.request().url()).pathname,
          body: route.request().postDataJSON(),
        };
        requests.push(request);
        submitted.resolve(request);
        await response.promise;
        state.tasks = [task];
        await route.fulfill({
          json: owned
            ? { ...episode, accepted_merge_task: task }
            : { operation_id: task.operation_id, status: "running" },
        });
      },
    );
    await page.evaluate(() => {
      window.location.hash = "#/cards";
    });
    await page.locator(".project-shelf").waitFor();
    await page.evaluate(() => new Promise(requestAnimationFrame));
    await page.evaluate(async () =>
      (await import("/src/artifacts/artifactViewerModel.ts")).openArtifact({
        projectId: "second",
        artifactId: "preview",
      }),
    );
    const viewer = page.locator(".artifact-viewer");
    await viewer.getByRole("button", { name: "Branch: main", exact: true }).click();
    const merge = page.getByRole("button", { name: `Merge ${ref.name}`, exact: true });
    assert.equal(await merge.isEnabled(), true);
    assert.equal(await merge.getAttribute("title"), "Merge");
    await merge.click();
    await page.locator("dialog").getByRole("button", { name: "Merge", exact: true }).click();
    const request = await submitted.promise;
    assert.equal(
      request.path,
      owned
        ? "/api/projects/second/episodes/chain-current/merge"
        : "/api/projects/second/graph-refs/member-fork/merge",
    );
    if (owned)
      assert.deepEqual(request.body, {
        request_id: request.body.request_id,
        history_mode: "merge",
        keep_branch_open: true,
        archive_graph_branch: false,
        remove_worktree: false,
        delete_code_branch: false,
      });
    else assert.deepEqual(request.body, { request_id: request.body.request_id });
    assert.match(request.body.request_id, /^[0-9a-f-]{36}$/);
    assert.equal(await page.locator("dialog").count(), 0);
    await viewer.getByRole("button", { name: "Close viewer", exact: true }).click();
    response.resolve();
    await page.locator(".toast").filter({ hasText: "Merge is running" }).waitFor();
    if (!owned)
      assert.ok(state.requests.includes(`/api/projects/second/tasks/${task.operation_id}`));
    assert.equal(
      state.requests.some((path) => path === `/api/projects/demo/tasks/${task.operation_id}`),
      false,
    );
    assert.equal(requests.length, 1);
    await page.evaluate(() => {
      window.location.hash = "/projects/second?view=overview";
    });
    await page.getByRole("button", { name: "Project menu", exact: true }).click();
    await page
      .getByRole("button", { name: "Project history, task in progress", exact: true })
      .waitFor();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    assert.deepEqual(errors, []);
  });
}

test("App picker retries an ambiguous episode admission with the original request UUID", async (t) => {
  const { ref, episode } = mergeFixture();
  const { page, state } = await openedShell(t, {
    connected: true,
    graphRefs: [mainRef(), ref],
    episodes: [episode],
  });
  const requests = [];
  const admitted = new Map();
  const task = mergeTask("merge-retry");
  await page.route(/\/api\/projects\/demo\/(episodes|graph-refs)\/[^/]+\/merge$/, async (route) => {
    const body = route.request().postDataJSON();
    requests.push({ path: new URL(route.request().url()).pathname, body });
    if (!admitted.has(body.request_id)) admitted.set(body.request_id, task);
    if (requests.length === 1)
      return route.fulfill({
        status: 503,
        json: { detail: "Merge admission response unavailable" },
      });
    state.tasks = [admitted.get(body.request_id)];
    await route.fulfill({
      json: { ...episode, accepted_merge_task: admitted.get(body.request_id) },
    });
  });
  await submitPickerMerge(page, ref);
  await page
    .locator(".toast.error")
    .filter({ hasText: "Merge admission response unavailable" })
    .waitFor();
  await submitPickerMerge(page, ref);
  await page.locator(".toast").filter({ hasText: "Merge is running" }).waitFor();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].path, "/api/projects/demo/episodes/chain-current/merge");
  assert.deepEqual(requests[1], requests[0]);
  assert.equal(admitted.size, 1);
});

test("App run-card merge uses the episode dispatcher and preserves explicit cleanup choices", async (t) => {
  const { ref, episode } = mergeFixture();
  const { page, state, errors } = await openedShell(t, {
    connected: true,
    graphRefs: [mainRef(), ref],
    episodes: [episode],
  });
  const task = mergeTask("merge-run-card");
  const submitted = Promise.withResolvers();
  await page.route("**/episodes/chain-current/merge-preview", (route) =>
    route.fulfill({
      json: {
        delivered_baseline: null,
        graph: { direct: 1, conflicts: 0, reviews: 0, paths: [] },
        code: {
          repo_alias: "code",
          source_branch: "work",
          target_branch: "main",
          commits_ahead: 1,
          leftover_files: [],
          status: "clean",
          conflict_files: [],
        },
        needs_agent: false,
      },
    }),
  );
  await page.route(/\/api\/projects\/demo\/(episodes|graph-refs)\/[^/]+\/merge$/, async (route) => {
    submitted.resolve({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON(),
    });
    state.tasks = [task];
    await route.fulfill({ json: { ...episode, accepted_merge_task: task } });
  });
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  const card = page.locator('.campaign-run[data-episode-id="chain-current"]');
  await card.waitFor();
  await card.locator(".campaign-run-toggle").click();
  const merge = card.locator(".episode-merge-panel");
  await merge.getByRole("button", { name: "Merge", exact: true }).click();
  const choices = merge.getByRole("group", { name: "Merge options", exact: true });
  await choices.waitFor();
  await choices
    .getByRole("checkbox", { name: "Keep the graph branch for more runs", exact: true })
    .check();
  await choices.getByRole("button", { name: "Merge", exact: true }).click();
  const request = await submitted.promise;
  assert.equal(request.path, "/api/projects/demo/episodes/chain-current/merge");
  assert.deepEqual(request.body, {
    request_id: request.body.request_id,
    target_branch: "main",
    history_mode: "merge",
    keep_branch_open: true,
    archive_graph_branch: false,
    remove_worktree: true,
    delete_code_branch: true,
  });
  assert.match(request.body.request_id, /^[0-9a-f-]{36}$/);
  await page.locator(".toast").filter({ hasText: "Merge is running" }).waitFor();
  assert.deepEqual(errors, []);
});

test("App preserves an accepted fork merge UUID when its task-detail GET refuses", async (t) => {
  const { ref } = mergeFixture(false);
  const { page, state } = await openedShell(t, {
    connected: true,
    graphRefs: [mainRef(), ref],
  });
  const requests = [];
  const task = mergeTask("accepted-fork-task", ref.branch_id);
  let detailAvailable = false;
  await page.route("**/graph-refs/member-fork/merge", async (route) => {
    requests.push(route.request().postDataJSON());
    state.tasks = [task];
    await route.fulfill({ json: { operation_id: task.operation_id, status: "running" } });
  });
  await page.route("**/tasks/accepted-fork-task", (route) =>
    detailAvailable
      ? route.fulfill({ json: task })
      : route.fulfill({ status: 404, json: { detail: "Task details temporarily unavailable" } }),
  );
  await submitPickerMerge(page, ref);
  await page
    .locator(".toast.error")
    .filter({ hasText: "Merge was accepted, but task details could not be loaded" })
    .waitFor();
  detailAvailable = true;
  await submitPickerMerge(page, ref);
  await page.locator(".toast").filter({ hasText: "Merge is running" }).waitFor();
  assert.equal(requests.length, 2);
  assert.match(requests[0].request_id, /^[0-9a-f-]{36}$/);
  assert.deepEqual(requests[1], requests[0]);
  assert.equal(state.tasks.length, 1);
  await page
    .getByRole("button", { name: "Project history, task in progress", exact: true })
    .waitFor();
});
