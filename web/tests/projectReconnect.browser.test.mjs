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
  const context = await browser.newContext();
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
    reconnectLoads: 0,
    reconnectObservations: 0,
    reconnectRequested: Promise.withResolvers(),
    graphRefs: options.graphRefs ?? [],
    branchGate: null,
    branchNames: {},
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
    const url = new URL(route.request().url());
    const path = url.pathname;
    const branchId = url.searchParams.get("branch_id");
    state.requests.push(branchId ? `${path}?branch_id=${branchId}` : path);
    const projectId = path.split("/")[3];
    let json = [];
    if (path === "/api/health" || path === "/api/health/details") {
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
    } else if (path.endsWith("/graph-refs")) json = state.graphRefs;
    else if (path === "/api/projects")
      json = ["demo", "second"].map((id) => ({ id, name: project(id).name, revision: 1 }));
    else if (/^\/api\/projects\/[^/]+$/.test(path) || path.endsWith("/cached")) {
      if (state.connected) {
        state.reconnectLoads++;
        state.reconnectRequested.resolve();
      }
      if (state.projectStatus !== 200)
        return route.fulfill({ status: state.projectStatus, json: { detail: "unavailable" } });
      const snapshot = project(
        projectId,
        branchId ? { kind: "branch", branch_id: branchId } : mainTarget,
      );
      if (state.projectGate) await state.projectGate.promise;
      if (branchId && state.branchGate) await state.branchGate.promise;
      if (!state.admitted) return route.fulfill({ status: 403, json: { detail: "denied" } });
      json = snapshot;
    } else if (path.endsWith("/readiness")) json = project(projectId);
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
  const authoritativeLoaded = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/projects/demo",
  );
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
    const reconciled = page.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith("/api/projects/demo"),
    );
    await reverify(page);
    assert.equal(await shellStillMounted(page), true);
    assert.equal(await page.getByRole("alertdialog").isVisible(), true);
    assert.equal(state.requests.filter((path) => path === "/api/projects/demo").length, 0);
    state.identityGate.resolve();
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    await reconciled;
    await page.locator(".project-reconciliation").waitFor({ state: "detached" });
    assert.equal(state.requests.filter((path) => path === "/api/projects/demo").length, 1);
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
    `a changed backend ${field} clears the project even after reconnect`,
    { timeout: 20000 },
    async (t) => {
      const { page, state } = await openedShell(t);
      state.health = { ...HEALTH, [field]: "replacement" };
      state.admitted = false;
      await reverify(page);
      await page.locator(".reconnect-state").waitFor();
      assert.equal(await shellStillMounted(page), false);
      assert.equal(await page.getByRole("alertdialog").count(), 0);
      const refused = page.waitForResponse(
        (response) => new URL(response.url()).pathname === "/api/projects/demo",
      );
      await page.locator(".reconnect-state button").click();
      await refused;
      await page.locator(".app-loading").waitFor({ state: "detached" });
      assert.equal(await page.locator(".app-shell").count(), 0);
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
      const requested = page.waitForRequest((request) =>
        new URL(request.url()).pathname.endsWith("/cached"),
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
        (request) => new URL(request.url()).pathname === "/api/projects/second/cached",
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
    const refused = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/projects/demo",
    );
    await reverify(page);
    await refused;
    await page.locator(".project-reconciliation").waitFor({ state: "detached" });
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    assert.equal(await shellStillMounted(page), true);
    assert.equal(await page.locator(".project-panel[inert]").count(), 0);
    state.health = null;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    state.projectStatus = 200;
    state.health = HEALTH;
    const recovered = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/projects/demo",
    );
    await reverify(page);
    await recovered;
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
    assert.equal(await shellStillMounted(page), true);
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
  assert.ok(state.requests.includes("/api/projects/demo/cached"));
});

test(
  "a reconciliation interrupted by another identity check drops its late snapshot",
  { timeout: 20000 },
  async (t) => {
    const { page, state } = await openedShell(t);
    state.health = null;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    state.projectName = "Interrupted response";
    state.projectGate = Promise.withResolvers();
    state.health = HEALTH;
    const started = page.waitForRequest(
      (request) => new URL(request.url()).pathname === "/api/projects/demo",
    );
    await reverify(page);
    await started;
    state.health = null;
    await reverify(page);
    await page.getByRole("alertdialog").waitFor();
    const lateResponse = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/projects/demo",
    );
    state.projectGate.resolve();
    await (await lateResponse).finished();
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
    state.health = HEALTH;
    await reverify(page);
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
  title: null,
});

test(
  "a ref switch keeps the view and shell, and drops a late response for the ref left",
  { timeout: 25000 },
  async (t) => {
    const main = {
      ...branchRef("main"),
      kind: "main",
      branch_id: null,
      episode_id: null,
      current_episode_id: null,
      base_head: null,
      head: { target: { kind: "main" }, revision: 1, transition_id: null },
      merge_state: null,
    };
    const { page, errors, state } = await openDemoProject(t, "fresh", {
      graphRefs: [main, branchRef("b1"), branchRef("b2")],
    });
    const picker = page.locator(".branch-graph-banner select");
    await picker.waitFor();
    await page.locator(".project-panel[inert]").waitFor({ state: "detached" });
    await page.getByRole("button", { name: "Agents", exact: true }).click();
    await page.evaluate(() => {
      window.__projectNode = document.querySelector(".project-tabs");
      window.__sawLoadingScreen = false;
      new MutationObserver(() => {
        if (document.querySelector(".app-loading")) window.__sawLoadingScreen = true;
      }).observe(document.body, { childList: true, subtree: true });
    });
    const agents = page.getByRole("button", { name: "Agents", exact: true });

    // The new ref's snapshot is pending: the shell and view stay, the panel is inert.
    state.branchGate = Promise.withResolvers();
    const b1Requested = page.waitForRequest(
      (request) => new URL(request.url()).searchParams.get("branch_id") === "b1",
    );
    await picker.selectOption(":branch:b1");
    await b1Requested;
    await page.locator(".project-panel[inert]").waitFor();
    assert.equal(await page.evaluate(() => window.__projectNode.isConnected), true);
    assert.equal(await agents.getAttribute("aria-current"), "page");
    const hash = new URLSearchParams((await page.evaluate(() => location.hash)).split("?")[1]);
    assert.equal(hash.get("view"), "chats");
    assert.equal(hash.get("branch_id"), "b1");
    state.branchGate.resolve();
    state.branchGate = null;
    await page.locator(".project-panel[inert]").waitFor({ state: "detached" });
    assert.equal(await agents.getAttribute("aria-current"), "page");

    // Leave b2 before its snapshot answers; the late answer must not paint on main.
    state.branchGate = Promise.withResolvers();
    state.branchNames.b2 = "Late branch response";
    const b2Requested = page.waitForRequest(
      (request) => new URL(request.url()).searchParams.get("branch_id") === "b2",
    );
    await picker.selectOption(":branch:b2");
    await b2Requested;
    await picker.selectOption({ index: 0 });
    await page.locator(".project-panel[inert]").waitFor({ state: "detached" });
    const late = page.waitForResponse(
      (response) => new URL(response.url()).searchParams.get("branch_id") === "b2",
    );
    state.branchGate.resolve();
    await (await late).finished();
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
    assert.equal(await agents.getAttribute("aria-current"), "page");
    assert.equal(await page.evaluate(() => window.__projectNode.isConnected), true);
    assert.equal(await page.evaluate(() => window.__sawLoadingScreen), false);
    assert.deepEqual(
      errors.filter((message) => !message.includes("Failed to load resource")),
      [],
    );
  },
);
