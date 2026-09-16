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

const target = { kind: "main", branch_id: null };
const rulesetTag = "rcp.lifecycle.v2";
const priorTransition = "a".repeat(64);
const previewTransition = "b".repeat(64);
const counts = {
  pending_proposals: 0,
  decisions_awaiting_choice: 2,
  open_blockers: 0,
  asserted: 0,
  accepted: 0,
  contested: 0,
};

function decision(id, title, status = "ready", extra = {}) {
  return {
    id,
    type: "decision",
    title,
    question: `Which way for ${title}?`,
    options: ["Keep the plan", "Amend the plan"],
    selected_option: null,
    rationale: null,
    consequences: [],
    status,
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
    ...extra,
  };
}

function graph(revision, nodes) {
  return {
    revision,
    nodes: Object.fromEntries(nodes.map((node) => [node.id, node])),
    edges: {},
    proposals: {},
    ambiguities: {},
    glossary: {},
    ontology: { types: [], fields: [], relations: [] },
    validation_messages: [],
    belief_transitions: [],
    replay_status: "complete",
  };
}

function attention(decisionIds) {
  return {
    pending_proposal_ids: [],
    decisions_awaiting_choice_ids: decisionIds,
    open_blocker_ids: [],
    proposal_actions: {},
    decision_prior_choices: {},
  };
}

test(
  "a staged decision keeps its candidate Inbox across an authoritative re-poll",
  { timeout: 30000 },
  async (t) => {
    const context = await browser.newContext();
    t.after(() => context.close());
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });

    let lastRemoteSyncAt = "2026-09-01T12:00:00Z";
    let experimentControl = {};
    let candidateAwaiting = ["dec/two"];
    let previewResponses = 0;
    const profile = { provider: "codex", model: "", reasoning: "high", run_on: "local" };
    // The head a real project snapshot carries: this revision, no transition id.
    const project = () => ({
      id: "demo",
      name: "Decided project",
      revision: 3,
      graph_target: target,
      graph_head: { target, revision: 3, transition_id: null },
      snapshot_freshness: "fresh",
      last_remote_sync_at: lastRemoteSyncAt,
      home_space_id: "space",
      canonical_state: { remote: true, reachable: true },
      graph_mutation: { available: true, reason: null },
      run_on: "local",
      repositories: [],
      machines: [{ alias: "local", host: "" }],
      project_truth_scope: [],
      default_run_truth_scope: [],
      providers: {},
      agent_profiles: Object.fromEntries(
        ["seed", "refresh", "node_chat", "project_chat", "paper_coach", "orchestrator"].map((k) => [
          k,
          profile,
        ]),
      ),
      provider_readiness: { local: {} },
      provider_skill_inventories: {},
      skill_catalog: [],
      skill_defaults: {},
      experiment_control: experimentControl,
      attention: attention(["dec/one", "dec/two"]),
      counts,
      graph: graph(3, [
        decision("dec/one", "Amend the preregistered analysis plan"),
        decision("dec/two", "Pre-main secondary diagnostics"),
      ]),
      paper: { text: "", sections: [] },
      paper_coach: {},
      validation_messages: [],
    });
    const candidateDecision = (id, title) =>
      candidateAwaiting.includes(id)
        ? decision(id, title)
        : decision(id, title, "decided", {
            selected_option: "Amend the plan",
            standing: "accepted",
            updated_rev: 4,
          });
    // The head a real preview carries: the same revision, named by the last
    // accepted transition.
    const previewPayload = () => ({
      projection: {
        head: { target, revision: 4, transition_id: previewTransition },
        base_head: { target, revision: 3, transition_id: priorTransition },
        graph: graph(4, [
          candidateDecision("dec/one", "Amend the preregistered analysis plan"),
          candidateDecision("dec/two", "Pre-main secondary diagnostics"),
        ]),
        attention: attention(candidateAwaiting),
        primary_question: null,
        counts: { ...counts, decisions_awaiting_choice: candidateAwaiting.length },
        experiment_control: experimentControl,
        ruleset_tag: rulesetTag,
        transition_id: previewTransition,
        canonical: false,
      },
      transition: {
        transition_id: previewTransition,
        pre_head: { target, revision: 3, transition_id: priorTransition },
        ruleset_tag: rulesetTag,
      },
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
        json = [{ id: "demo", name: "Decided project", revision: 3 }];
      else if (path === "/api/projects/demo" || path.endsWith("/cached")) json = project();
      else if (path.endsWith("/readiness"))
        // Readiness answers once and settles: an empty provider matrix would ask
        // again on every project replacement.
        json = {
          compute_status: {},
          provider_logins: [],
          provider_readiness: { local: { codex: { available: true, reason: null } } },
          providers: { codex: { available: true } },
          provider_skill_inventories: {},
          agent_profiles: project().agent_profiles,
        };
      else if (path.endsWith("/revision"))
        json = {
          revision: 3,
          snapshot_freshness: "fresh",
          last_remote_sync_at: lastRemoteSyncAt,
          graph_mutation: { available: true, reason: null },
        };
      else if (path.endsWith("/transition-manifest"))
        json = {
          ruleset_tag: rulesetTag,
          triggers: [
            {
              operation: "update_nodes",
              node_types: ["decision"],
              node_fields: ["selected_option", "status"],
              relations: [],
            },
          ],
        };
      else if (path.endsWith("/sync/preview")) {
        previewResponses += 1;
        // The refetch after a re-poll is slow enough to see what the app renders
        // while it is in flight: the candidate must not fall back to canonical.
        if (previewResponses > 1) await new Promise((resolve) => setTimeout(resolve, 2000));
        json = previewPayload();
      } else if (path.endsWith("/chats")) json = { chats: [], next_cursor: null };
      else if (path.endsWith("/usage")) json = { tasks: [], totals: {}, by_provider: [] };
      await route.fulfill({ json });
    });

    await page.goto(`${origin}/#/projects/demo?view=attention`);
    const inboxOpen = page.getByRole("heading", { name: "Inbox" }).locator("..").getByText("open");
    await inboxOpen.waitFor();
    assert.equal((await inboxOpen.textContent()).trim(), "2 open");

    const previewApplied = page.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith("/sync/preview"),
    );
    await page
      .getByRole("button", { name: "Amend the preregistered analysis plan" })
      .first()
      .click();
    await page.getByRole("radio", { name: "Amend the plan" }).first().click();
    await (await previewApplied).finished();
    await page.waitForFunction(
      () => !document.body.textContent.includes("Preparing staged transition preview."),
    );
    assert.equal((await inboxOpen.textContent()).trim(), "1 open");

    // Operational polling re-reads the project at the same revision, and this
    // read carries control state that moved. The staged decision must stay
    // decided in the Inbox while the refetch is in flight.
    const reloaded = page.waitForResponse(
      (response) => new URL(response.url()).pathname === "/api/projects/demo",
    );
    const refetched = page.waitForResponse((response) =>
      new URL(response.url()).pathname.endsWith("/sync/preview"),
    );
    lastRemoteSyncAt = "2026-09-01T12:01:00Z";
    experimentControl = { "exp/one": { health: "completed" } };
    candidateAwaiting = [];
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await (await reloaded).finished();
    await page.waitForTimeout(500);

    assert.equal((await inboxOpen.textContent()).trim(), "1 open");

    // The refetch replaces the whole candidate, so what moved is delivered
    // without ever rendering canonical state under the staged edit.
    await (await refetched).finished();
    await page.waitForFunction(
      () => !document.body.textContent.includes("Preparing staged transition preview."),
    );
    assert.equal((await inboxOpen.textContent()).trim(), "0 open");
    assert.deepEqual(errors, []);
  },
);
