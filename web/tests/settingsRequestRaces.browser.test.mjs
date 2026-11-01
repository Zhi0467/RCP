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
  await ceilingEditor(page).waitFor();
  return page;
}

// A form field each project restores with its own value.
const ceilingEditor = (page) => page.locator(".agent-auto-research-default input");

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

async function savedProject(page, ceiling = 30) {
  return page.evaluate((value) => {
    const project = window.settingsProject("alpha");
    project.default_auto_research_invocation_ceiling = value;
    return project;
  }, ceiling);
}

const operations = [
  {
    name: "Save",
    path: "settings",
    prepare: (page, value) => ceilingEditor(page).fill(value),
    click: (page) => page.getByRole("button", { name: "Save", exact: true }).click(),
    pending: (page) => page.getByRole("button", { name: "Saving", exact: true }),
    result: (page) => savedProject(page),
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
        await operation.prepare(page, "11");
        await operation.click(page);
        const oldRoute = await old.started;

        await page.getByRole("button", { name: "Open beta", exact: true }).click();
        const editor = ceilingEditor(page);
        await page.waitForFunction(
          () => document.querySelector(".agent-auto-research-default input").value === "20",
        );
        assert.equal(await operation.pending(page).count(), operation.name === "Save" ? 0 : 1);
        if (operation.name !== "Save")
          assert.equal(await operation.pending(page).isDisabled(), false);
        await editor.fill("21");
        let currentRoute;
        if (returns) {
          // An id-only fence would admit the old request after this return to A.
          await page.getByRole("button", { name: "Open alpha", exact: true }).click();
          await page.getByLabel("Current project").filter({ hasText: "alpha" }).waitFor();
          await editor.fill("12");
          const current = await holdRequest(page, operation.path);
          await operation.prepare(page, "12");
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
        assert.equal(await editor.inputValue(), returns ? "12" : "21");
        assert.doesNotMatch(
          await page.locator(".settings-save-status").textContent(),
          /Previous visit failed|\bSaved|cleared/i,
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

test("Agents can write applies at once and leaves the Save form clean", async (t) => {
  const page = await openFixture(t);
  const writes = [];
  await page.route("**/api/projects/alpha/repositories/*/writable", async (route) => {
    writes.push({
      method: route.request().method(),
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON(),
    });
    const saved = await savedProject(page, 10);
    saved.project_truth_scope = ["repo", "data"];
    await route.fulfill({ json: saved });
  });
  // The canonical state repository is always writable.
  const state = page.getByRole("checkbox", { name: "Agents can write repo" });
  assert.equal(await state.isChecked(), true);
  assert.equal(await state.isDisabled(), true);

  const data = page.getByRole("checkbox", { name: "Agents can write data" });
  assert.equal(await data.isChecked(), false);
  await data.click();
  await page.waitForFunction(() => window.settingsPublications.length === 1);
  assert.deepEqual(writes, [
    {
      method: "PUT",
      path: "/api/projects/alpha/repositories/data/writable",
      body: { writable: true },
    },
  ]);
  assert.equal(await data.isChecked(), true);
  assert.equal(await page.getByRole("button", { name: "Save", exact: true }).isDisabled(), true);
});

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

  // Leaving alpha cancels its dormant read when no other owner watches it.
  // A surviving response is fenced just as a cancellation is; neither may
  // replace beta's members or its invitation candidates.
  await alphaRoute.fulfill({ json: members("alpha-member") });
  const alphaResponse = await alphaRoute.request().response();
  if (alphaResponse) await alphaResponse.finished();
  else assert.match(alphaRoute.request().failure().errorText, /abort/i);
  await page.evaluate(() => new Promise(requestAnimationFrame));
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
  await ceilingEditor(page).fill("11");
  const deferred = await holdRequest(page, "settings");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const route = await deferred.started;
  await page.getByRole("button", { name: "Toggle settings", exact: true }).click();
  await page.locator(".settings-page").waitFor({ state: "detached" });
  await respond(page, route, { json: await savedProject(page) });
  assert.deepEqual(await page.evaluate(() => window.settingsPublications), []);
});
