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

async function openFixture(t, { query = "", configure = async () => undefined } = {}) {
  const context = await browser.newContext({ reducedMotion: "reduce" });
  t.after(() => context.close());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.route("**/api/space/users", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/projects/*/members", (route) => route.fulfill({ json: [] }));
  await configure(page);
  await page.goto(`${origin}/tests/fixtures/settingsRequestRaces.html${query}`);
  await page.getByLabel("Codex executable on local").waitFor();
  return page;
}

async function holdRequest(page, suffix) {
  const { promise, resolve } = Promise.withResolvers();
  await page.route(`**/api/projects/alpha/${suffix}`, resolve, { times: 1 });
  return { started: promise };
}

async function respond(page, route, options) {
  const response = page.waitForResponse((item) => item.request() === route.request());
  await route.fulfill(options);
  await (await response).finished();
  // Let React commit updates queued by the completed API response.
  await page.evaluate(() => new Promise(requestAnimationFrame));
}

async function savedProject(page, path = "/alpha/saved-codex") {
  return page.evaluate((binaryPath) => {
    const project = window.settingsProject("alpha");
    project.machines[0].provider_paths.codex = binaryPath;
    return project;
  }, path);
}

const operations = [
  {
    name: "Save",
    path: "settings",
    prepare: (page, value) => page.getByLabel("Codex executable on local").fill(value),
    click: (page) => page.getByRole("button", { name: "Save", exact: true }).click(),
    pending: (page) => page.getByRole("button", { name: "Saving", exact: true }),
    result: (page) => savedProject(page),
  },
  {
    name: "Resolve",
    path: "machines/local/providers/codex/resolve",
    prepare: async () => undefined,
    click: (page) => page.getByRole("button", { name: "Resolve", exact: true }).click(),
    pending: (page) => page.getByRole("button", { name: "Resolve", exact: true }),
    result: async (page) => {
      const project = await savedProject(page);
      return {
        project,
        machine: "local",
        provider: "codex",
        binary_path: "/alpha/resolved-codex",
        readiness: project.providers.codex,
      };
    },
  },
  {
    name: "Clear project cache",
    path: "caches",
    prepare: async () => undefined,
    click: (page) => page.getByRole("button", { name: "Clear project cache", exact: true }).click(),
    pending: (page) => page.getByRole("button", { name: "Clear project cache", exact: true }),
    result: async (page) => {
      const { cache_metrics: metrics } = await savedProject(page);
      for (const metric of Object.values(metrics)) Object.assign(metric, { bytes: 0, count: 0 });
      return metrics;
    },
  },
];

for (const operation of operations) {
  for (const fails of [false, true]) {
    for (const returns of [false, true]) {
      test(`${operation.name} ${fails ? "failure" : "success"} is ignored after ${returns ? "A to B to A" : "A to B"}`, async (t) => {
        const page = await openFixture(t);
        const old = await holdRequest(page, operation.path);
        await operation.prepare(page, "/alpha/first-edit");
        await operation.click(page);
        const oldRoute = await old.started;

        await page.getByRole("button", { name: "Open beta", exact: true }).click();
        const editor = page.getByLabel("Codex executable on local");
        await page.waitForFunction(
          () =>
            document.querySelector('[aria-label="Codex executable on local"]').value ===
            "/beta/codex",
        );
        assert.equal(await operation.pending(page).count(), operation.name === "Save" ? 0 : 1);
        if (operation.name !== "Save")
          assert.equal(await operation.pending(page).isDisabled(), false);
        await editor.fill("/beta/unsaved-edit");
        let currentRoute;
        if (returns) {
          // An id-only fence would admit the old request after this return to A.
          await page.getByRole("button", { name: "Open alpha", exact: true }).click();
          await page.getByLabel("Current project").filter({ hasText: "alpha" }).waitFor();
          await editor.fill("/alpha/new-visit-edit");
          const current = await holdRequest(page, operation.path);
          await operation.prepare(page, "/alpha/new-visit-edit");
          await operation.click(page);
          currentRoute = await current.started;
        }
        await respond(
          page,
          oldRoute,
          fails
            ? { status: 500, json: { detail: "Previous visit failed" } }
            : { json: await operation.result(page) },
        );
        assert.equal(
          await editor.inputValue(),
          returns ? "/alpha/new-visit-edit" : "/beta/unsaved-edit",
        );
        assert.doesNotMatch(
          await page.locator(".settings-save-status").textContent(),
          /Previous visit failed|\bSaved|resolved|cleared/i,
        );
        assert.deepEqual(await page.evaluate(() => window.settingsPublications), []);
        if (currentRoute) {
          assert.equal(await operation.pending(page).isDisabled(), true);
          await respond(page, currentRoute, { json: await operation.result(page) });
          await page.waitForFunction(() => window.settingsPublications.length === 1);
        }
      });
    }
  }
}

test("project member responses and invite selections stay with their project", async (t) => {
  const users = [
    { user_id: "self", display_name: "Researcher" },
    { user_id: "alpha-member", display_name: "Alice" },
    { user_id: "beta-member", display_name: "Ben" },
    { user_id: "candidate", display_name: "Clara" },
  ];
  const members = (id) =>
    users
      .filter((user) => user.user_id === "self" || user.user_id === id)
      .map((user) => ({ ...user, seated_at: new Date().toISOString() }));
  let initialAlpha;
  const page = await openFixture(t, {
    query: "?team",
    configure: async (page) => {
      await page.route("**/api/server-status", (route) =>
        route.fulfill({ status: 503, json: { detail: "Server status unavailable in fixture." } }),
      );
      await page.route("**/api/space/users", (route) => route.fulfill({ json: users }));
      await page.route("**/api/projects/alpha/members", (route) =>
        route.fulfill({ json: members("alpha-member") }),
      );
      await page.route("**/api/projects/beta/members", (route) =>
        route.fulfill({ json: members("beta-member") }),
      );
      initialAlpha = await holdRequest(page, "members");
    },
  });
  const alphaRoute = await initialAlpha.started;
  await page.getByRole("button", { name: "Open beta", exact: true }).click();
  const memberList = page.locator(".project-member-list");
  await memberList.getByText("Ben", { exact: true }).waitFor();
  const invitation = page.getByLabel("Invite a member of this space");
  assert.deepEqual(await invitation.locator("option").allTextContents(), [
    "Invite member…",
    "Alice",
    "Clara",
  ]);

  await respond(page, alphaRoute, { json: members("alpha-member") });
  assert.match(await memberList.textContent(), /Ben/);
  assert.doesNotMatch(await memberList.textContent(), /Alice/);
  assert.deepEqual(await invitation.locator("option").allTextContents(), [
    "Invite member…",
    "Alice",
    "Clara",
  ]);

  await invitation.selectOption("candidate");
  assert.equal(await page.getByRole("button", { name: "Invite", exact: true }).isDisabled(), false);
  await page.getByRole("button", { name: "Open alpha", exact: true }).click();
  await memberList.getByText("Alice", { exact: true }).waitFor();
  assert.equal(await invitation.inputValue(), "");
  assert.equal(await page.getByRole("button", { name: "Invite", exact: true }).isDisabled(), true);
  assert.deepEqual(await invitation.locator("option").allTextContents(), [
    "Invite member…",
    "Ben",
    "Clara",
  ]);
});

test("a Save response cannot publish after Settings has closed", async (t) => {
  const page = await openFixture(t);
  await page.getByLabel("Codex executable on local").fill("/alpha/sent-codex");
  const deferred = await holdRequest(page, "settings");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const route = await deferred.started;
  await page.getByRole("button", { name: "Toggle settings", exact: true }).click();
  await page.locator(".settings-page").waitFor({ state: "detached" });
  await respond(page, route, { json: await savedProject(page) });
  assert.deepEqual(await page.evaluate(() => window.settingsPublications), []);
});

test("Save preserves provider and compute edits typed while its response is pending", async (t) => {
  const page = await openFixture(t);
  const editor = page.getByLabel("Codex executable on local");
  const compute = page.getByRole("textbox", { name: "Name", exact: true });
  await editor.fill("/alpha/sent-codex");
  await compute.fill("Sent compute name");
  const first = await holdRequest(page, "settings");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const firstRoute = await first.started;
  await editor.fill("/alpha/newer-codex");
  await compute.fill("Newer compute name");
  const saved = await savedProject(page, "/alpha/sent-codex");
  saved.compute_connections[0].name = "Sent compute name";
  await respond(page, firstRoute, { json: saved });
  assert.equal(await editor.inputValue(), "/alpha/newer-codex");
  assert.equal(await compute.inputValue(), "Newer compute name");

  const second = await holdRequest(page, "settings");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const secondRoute = await second.started;
  const body = secondRoute.request().postDataJSON();
  assert.equal(body.machine_provider_paths.local.codex, "/alpha/newer-codex");
  assert.equal(body.compute_connections[0].name, "Newer compute name");
  saved.machines[0].provider_paths.codex = "/alpha/newer-codex";
  saved.compute_connections[0].name = "Newer compute name";
  await respond(page, secondRoute, { json: saved });
  await page.getByText("Saved.", { exact: true }).waitFor();
});

test("Resolve preserves a provider path edited after the lookup started", async (t) => {
  const page = await openFixture(t);
  const deferred = await holdRequest(page, "machines/local/providers/codex/resolve");
  await page.getByRole("button", { name: "Resolve", exact: true }).click();
  const route = await deferred.started;
  const editor = page.getByLabel("Codex executable on local");
  await editor.fill("/alpha/human-choice");
  await respond(page, route, { json: await operations[1].result(page) });
  assert.equal(await editor.inputValue(), "/alpha/human-choice");
  assert.equal(await page.getByRole("button", { name: "Save", exact: true }).isDisabled(), false);
});
