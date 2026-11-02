import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { message, mockApi, settled, transcript, withFixture } from "./fixtures/agentThreadsApi.mjs";
import { projectFor } from "./fixtures/settingsProject.ts";

/** A decodable snapshot of the moss thread's project, so its one graph read succeeds. */
const snapshot = {
  ...projectFor("p1"),
  graph_target: { kind: "main" },
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
};

const WINDOW_MS = 10_000;
const STEP_MS = 250;

const polled = JSON.parse(
  readFileSync(new URL("../../tests/fixtures/polled_routes.json", import.meta.url), "utf8"),
).routes.map((route) => route.path.split("/"));

/** True when a path matches a polled route template, segment by segment. */
const isPolled = (path) => {
  const segments = path.split("/");
  return polled.some(
    (template) =>
      template.length === segments.length &&
      template.every((part, index) => /^\{\w+\}$/.test(part) || part === segments[index]),
  );
};

/**
 * Opens a page on Playwright's fake clock and records every API request as
 * `METHOD /path`, with a window that counts only what the clock's steps cause.
 */
async function openPage(browser) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 844 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await mockApi(page, []);
  await page.route("**/api/projects/p1", (route) => route.fulfill({ json: snapshot }));
  const all = [];
  const pending = new Set();
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith("/api/")) return;
    all.push(`${request.method()} ${decodeURIComponent(path)}`);
    pending.add(request);
  });
  const done = (request) => pending.delete(request);
  page.on("requestfinished", done);
  page.on("requestfailed", done);
  await page.clock.install();
  /** Waits until no API request is open across three straight checks. */
  const settle = async () => {
    for (let quiet = 0; quiet < 3;) {
      await page.waitForTimeout(25);
      quiet = pending.size > 0 ? 0 : quiet + 1;
    }
  };
  /** Steps the paused clock through the window and returns the requests it caused. */
  const measure = async () => {
    await settle();
    await page.clock.pauseAt((await page.evaluate(() => Date.now())) + STEP_MS);
    await settle();
    const start = all.length;
    for (let elapsed = 0; elapsed < WINDOW_MS; elapsed += STEP_MS) {
      await page.clock.runFor(STEP_MS);
      await settle();
    }
    const counts = {};
    for (const request of all.slice(start)) counts[request] = (counts[request] ?? 0) + 1;
    return counts;
  };
  const setHidden = (hidden) =>
    page.evaluate((value) => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => (value ? "hidden" : "visible"),
      });
      document.dispatchEvent(new Event("visibilitychange"));
    }, hidden);
  return { page, all, errors, measure, setHidden };
}

/** Lists every API path the page read more than once that no polled route template covers. */
function unlistedRepeats(all) {
  const seen = new Map();
  for (const request of all) seen.set(request, (seen.get(request) ?? 0) + 1);
  return [...seen]
    .filter(([request, count]) => count > 1 && !isPolled(request.split(" ")[1]))
    .map(([request, count]) => `${request} x${count}`);
}

/** Fails with every request over its ceiling; a request without a ceiling allows none. */
function assertWithin(counts, ceilings) {
  const over = Object.entries(counts)
    .filter(([request, count]) => count > (ceilings[request] ?? 0))
    .map(([request, count]) => `${request} x${count} > ${ceilings[request] ?? 0}`);
  assert.deepEqual(over, []);
}

test("a repeated path passes only when a polled route template covers it", () => {
  assert.equal(isPolled("/api/agent-threads/moss/transcript"), true);
  assert.equal(isPolled("/api/agent-threads/moss/transcript/extra"), false);
  assert.equal(isPolled("/api/agent-threads/moss/turns"), false);
});

const BEAT = "POST /api/resource-watermarks";
const LIVE = "GET /api/projects/p1/tasks/op-live";

test("an idle open thread only beats over ten seconds", async () => {
  await withFixture(async (browser, base) => {
    const { page, all, errors, measure } = await openPage(browser);
    await page.goto(`${base}?wire#/agent`);
    await page.getByRole("region", { name: "Pip" }).locator(".chat-progress").waitFor();
    const counts = await measure();
    assert.ok(counts[BEAT] > 0);
    assertWithin(counts, { [BEAT]: 11 });
    assert.deepEqual(unlistedRepeats(all), []);
    assert.deepEqual(errors, []);
  });
});

test("a running thread polls only its task, and a hidden tab reads nothing", async () => {
  await withFixture(async (browser, base) => {
    const { page, all, errors, measure, setHidden } = await openPage(browser);
    await page.route("**/api/projects/p1/chats/moss", (route) =>
      route.fulfill({ json: transcript("moss", [message("n1", "user", "Run it", "op-live")]) }),
    );
    const running = { ...settled("op-live"), active: true, settled: false };
    await page.route("**/api/projects/p1/tasks/op-live", (route) =>
      route.fulfill({ json: running }),
    );
    await page.goto(`${base}?wire#/agent/moss`);
    await page.getByRole("region", { name: "Moss" }).locator(".agent-turn-live").waitFor();
    const visible = await measure();
    assert.ok(visible[LIVE] > 0);
    assertWithin(visible, { [BEAT]: 11, [LIVE]: 6 });
    await setHidden(true);
    assertWithin(await measure(), {});
    assert.deepEqual(unlistedRepeats(all), []);
    assert.deepEqual(errors, []);
  });
});

test("an idle project tab only beats over ten seconds", async () => {
  await withFixture(async (browser, base) => {
    const { page, all, errors, measure } = await openPage(browser);
    await page.goto(`${base}?wire&view=project`);
    await page
      .getByRole("region", { name: "Moss" })
      .getByRole("button", { name: "Approve" })
      .waitFor();
    const counts = await measure();
    assert.ok(counts[BEAT] > 0);
    assertWithin(counts, { [BEAT]: 11 });
    assert.deepEqual(unlistedRepeats(all), []);
    assert.deepEqual(errors, []);
  });
});
