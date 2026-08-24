import assert from "node:assert/strict";
import { withTaskAnswers } from "./taskAnswers.mjs";
import { readFileSync } from "node:fs";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { AttentionOverview, ExecutionView } = await server.ssrLoadModule(
  "/src/views/GraphViews.tsx",
);
const {
  decisionsAwaitingChoice,
  humanAttentionBlockers,
  shouldShowCoverageBoundaryWarning,
  taskRetryRequestBody,
} = await server.ssrLoadModule("/src/App.tsx");
const { AttentionRail } = await server.ssrLoadModule("/src/components/AttentionRail.tsx");
const { ProjectSettings } = await server.ssrLoadModule("/src/views/ProjectSettings.tsx");

after(() => server.close());

test("Experiment provider-switch retry overrides never submit run_on", () => {
  const task = {
    request: { patch_kind: "experiment_loop" },
  };
  const config = {
    provider: "claude",
    model: "claude-sonnet-4-5",
    reasoning: "high",
    run_on: "cluster",
  };

  assert.deepEqual(taskRetryRequestBody(task, config), {
    provider: "claude",
    model: "claude-sonnet-4-5",
    reasoning: "high",
  });
});

function graph(overrides = {}) {
  return {
    revision: 1,
    nodes: {},
    edges: {},
    proposals: {},
    ambiguities: {},
    glossary: {},
    validation_messages: [],
    belief_transitions: [],
    replay_status: "complete",
    replay_failure: null,
    ontology: { types: [], fields: [], relations: [] },
    ...overrides,
  };
}

function blocker(id, blockerType, status = "open", standing = "asserted") {
  return {
    id,
    type: "blocker",
    title: id,
    blocker_type: blockerType,
    status,
    standing,
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  };
}

function decision(id, status) {
  return {
    id,
    type: "decision",
    title: id,
    question: `Choose ${id}?`,
    options: ["First", "Second"],
    selected_option: status === "decided" || status === "revisit" ? "First" : null,
    status,
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  };
}

function task(operationId, kind, status, statusMessage, updatedAt = "2026-08-03T00:00:00Z") {
  return withTaskAnswers({
    operation_id: operationId,
    project_id: "project",
    kind,
    status,
    request: {},
    created_at: updatedAt,
    updated_at: updatedAt,
    status_message: statusMessage,
    attempt: 1,
    estimate_seconds: 60,
    estimate_samples: 1,
    phase: status,
    elapsed_seconds: 1,
    progress: 0.1,
    can_pause: false,
    can_resume: false,
    can_retry: false,
  });
}

test("Inbox counts pending proposals, queued Decisions, and only asserted open blockers", () => {
  const nodes = {
    asserted: blocker("ASSERTED OPEN", "scientific"),
    accepted: blocker("ACCEPTED OPEN", "infrastructure", "open", "accepted"),
    contested: blocker("CONTESTED OPEN", "design", "open", "contested"),
    resolved: blocker("ASSERTED RESOLVED", "design", "resolved"),
    decisionOpen: decision("OPEN DECISION", "open"),
    decisionReady: decision("READY DECISION", "ready"),
    decisionRevisit: decision("REVISIT DECISION", "revisit"),
  };
  assert.deepEqual(
    humanAttentionBlockers(Object.values(nodes)).map((node) => node.id),
    ["ASSERTED OPEN"],
  );

  const html = renderToStaticMarkup(
    React.createElement(AttentionOverview, {
      graph: graph({
        nodes,
        proposals: {
          pending: {
            id: "pending",
            title: "Pending",
            card: {},
            ops: [],
            related_node_ids: [],
            base_rev: 1,
            status: "pending",
          },
        },
        ambiguities: {
          open: {
            id: "open",
            question: "Open ambiguity",
            why_it_matters: "It matters",
            related_node_ids: [],
            status: "open",
          },
        },
      }),
      onSelectNode() {},
    }),
  );

  assert.match(html, /4 open/);
  assert.match(html, /Decisions awaiting choice<\/span><strong>2<\/strong>/);
  assert.match(html, /Blockers awaiting judgment<\/span><strong>1<\/strong>/);
  assert.doesNotMatch(html, /Open ambiguities|Open blockers|Scientific blockers|Resolve “/);
});

test("Decision attention membership uses canonical status while rendering staged nodes", () => {
  const statuses = ["open", "ready", "decided", "revisit", "superseded"];
  const canonicalNodes = Object.fromEntries(
    statuses.map((status) => [status, decision(status.toUpperCase(), status)]),
  );
  const presentedNodes = Object.fromEntries(
    Object.entries(canonicalNodes).map(([status, node]) => [
      node.id,
      {
        ...node,
        title: `STAGED ${status.toUpperCase()}`,
        status: status === "ready" || status === "revisit" ? "decided" : node.status,
        draft_touched: true,
      },
    ]),
  );

  assert.deepEqual(
    decisionsAwaitingChoice(Object.values(canonicalNodes), presentedNodes).map((node) => [
      node.id,
      node.title,
      node.status,
      node.draft_touched,
    ]),
    [
      ["READY", "STAGED READY", "ready", true],
      ["REVISIT", "STAGED REVISIT", "revisit", true],
    ],
  );
  assert.deepEqual(decisionsAwaitingChoice(Object.values(presentedNodes), presentedNodes), []);
});

test("Decision attention rows show only title and state and open the existing node card", () => {
  const selected = [];
  const decisions = [decision("READY ROW", "ready"), decision("REVISIT ROW", "revisit")];
  const props = {
    decisions,
    blockers: [],
    onSelectNode(nodeId) {
      selected.push(nodeId);
    },
  };
  const html = renderToStaticMarkup(React.createElement(AttentionRail, props));

  assert.match(html, /READY ROW/);
  assert.match(html, /REVISIT ROW/);
  assert.match(html, />Ready<\/span>/);
  assert.match(html, />Revisit<\/span>/);
  assert.doesNotMatch(html, /First|Second|Resolve|Dismiss|ambiguity/i);

  const tree = AttentionRail(props);
  const readyRow = findElement(tree, (element) => element.key === "READY ROW");
  assert.ok(readyRow);
  readyRow.props.onClick();
  assert.deepEqual(selected, ["READY ROW"]);
});

test("a successful Seed or Refresh suppresses the unseeded coverage warning", () => {
  const coverage = {
    repositories_never_seen: ["repo-a"],
    sessions_skipped: [],
  };

  assert.equal(shouldShowCoverageBoundaryWarning({ coverage, last_refresh_at: null }), true);
  assert.equal(
    shouldShowCoverageBoundaryWarning({
      coverage: { ...coverage, note: "No seed has completed." },
      last_refresh_at: "2026-08-06T10:00:00Z",
    }),
    false,
  );
  assert.equal(
    shouldShowCoverageBoundaryWarning({
      coverage: {
        ...coverage,
        note: "One source thread was skipped.",
        sessions_skipped: ["repo-a/session-1"],
      },
      last_refresh_at: "2026-08-06T10:00:00Z",
    }),
    true,
  );
});

test("staged blocker judgments remain until canonical standing changes", () => {
  const canonicalNodes = {
    agree: blocker("STAGED AGREE", "scientific"),
    contest: blocker("STAGED CONTEST", "design"),
  };
  const presentedNodes = {
    "STAGED AGREE": blocker("STAGED AGREE", "scientific", "open", "accepted"),
    "STAGED CONTEST": blocker("STAGED CONTEST", "design", "open", "contested"),
  };

  assert.deepEqual(
    humanAttentionBlockers(Object.values(canonicalNodes), presentedNodes).map((node) => [
      node.id,
      node.standing,
    ]),
    [
      ["STAGED AGREE", "accepted"],
      ["STAGED CONTEST", "contested"],
    ],
  );
  assert.deepEqual(humanAttentionBlockers(Object.values(presentedNodes), presentedNodes), []);
});

test("Runs needs action includes only asserted open blockers and excludes generic chat", () => {
  const html = renderToStaticMarkup(
    React.createElement(ExecutionView, {
      graph: graph({
        nodes: {
          stagedAccepted: blocker("STAGED ACCEPTED", "scientific", "open", "accepted"),
          stagedContested: blocker("STAGED CONTESTED", "design", "open", "contested"),
          stagedResolved: {
            ...blocker("STAGED RESOLVED", "scientific", "resolved"),
            draft_touched: true,
          },
          stagedReopened: blocker("STAGED REOPENED", "scientific"),
          accepted: blocker("ACCEPTED OPEN", "infrastructure", "open", "accepted"),
          contested: blocker("CONTESTED OPEN", "design", "open", "contested"),
          resolved: blocker("ASSERTED RESOLVED", "design", "resolved"),
        },
      }),
      attentionBlockerIds: new Set(["STAGED ACCEPTED", "STAGED CONTESTED", "STAGED RESOLVED"]),
      tasks: [
        task("refresh", "refresh", "failed", "REFRESH FAILURE", "2026-08-03T01:00:00Z"),
        task("seed", "seed", "succeeded", "SEED COMPLETE"),
        task("running-refresh", "refresh", "running", "REFRESH RUNNING"),
        task("node-chat", "node_chat", "failed", "NODE CHAT TRACEBACK"),
        task("project-chat", "project_chat", "running", "PROJECT CHAT RUNNING"),
        task("coach", "paper_coach", "failed", "PAPER COACH FAILURE"),
      ],
      watchers: [],
      experimentControl: {},
      dismissedTaskIds: new Set(),
      selectedExperimentId: null,
      focusExperimentId: null,
      runBusy: false,
      stopBusyId: null,
      onInspectTask() {},
      onDismissTask() {},
      onSelectNode() {},
      onSelectExperiment() {},
      onDetailFocused() {},
      onRunExperiment() {},
      onStopExperiment() {},
    }),
  );

  assert.doesNotMatch(html, /Runs &amp; experiments|As of/);
  assert.ok(html.indexOf(">Running<") < html.indexOf(">Needs action<"));
  assert.ok(html.indexOf(">Needs action<") < html.indexOf(">Completed<"));
  assert.match(html, /REFRESH RUNNING/);
  assert.match(html, /REFRESH FAILURE/);
  assert.match(html, /SEED COMPLETE/);
  assert.match(html, /STAGED ACCEPTED/);
  assert.match(html, /STAGED CONTESTED/);
  assert.match(html, /class="blocker-row draft-touched"[^>]*>.*STAGED RESOLVED/s);
  assert.doesNotMatch(
    html,
    /STAGED REOPENED|ACCEPTED OPEN|CONTESTED OPEN|ASSERTED RESOLVED|NODE CHAT TRACEBACK|PROJECT CHAT RUNNING|PAPER COACH FAILURE/,
  );
});

test("Runs renders a legacy cached Experiment control without an operational block", () => {
  const experiment = {
    id: "exp/legacy-cache",
    type: "experiment",
    title: "Legacy cached experiment",
    objective: "Keep old cached snapshots renderable.",
    design: "",
    expected_outcomes: [],
    interpretation_rules: [],
    completion_criteria: [],
    invocation_ceiling: 2,
    attempts: [],
    current_summary: "Cached summary",
    next_action: null,
    status: "planned",
    standing: "asserted",
    created_rev: 1,
    updated_rev: 1,
    source_refs: [],
    extension_fields: {},
  };
  const html = renderToStaticMarkup(
    React.createElement(ExecutionView, {
      graph: graph({ nodes: { [experiment.id]: experiment } }),
      attentionBlockerIds: new Set(),
      tasks: [],
      watchers: [],
      experimentControl: {
        [experiment.id]: {
          ready: true,
          reasons: [],
          invocations_used: 0,
          invocation_ceiling: 2,
          invocations_remaining: 2,
          episode_id: null,
          paused: false,
          active: false,
          governing_decisions: [],
          decision_drift: [],
        },
      },
      dismissedTaskIds: new Set(),
      selectedExperimentId: null,
      focusExperimentId: null,
      runBusy: false,
      stopBusyId: null,
      onInspectTask() {},
      onDismissTask() {},
      onSelectNode() {},
      onSelectExperiment() {},
      onDetailFocused() {},
      onRunExperiment() {},
      onStopExperiment() {},
    }),
  );

  assert.match(html, /Legacy cached experiment/);
  assert.match(html, /Start an episode/);
  assert.doesNotMatch(html, /Cached summary/);
});

test("Project Settings supports legacy profiles without an ontology authoring surface", () => {
  const storage = new Map();
  const previousLocalStorage = globalThis.localStorage;
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
  };
  try {
    const permissions = {
      read_graph: true,
      read_research_md: true,
      read_introduction: false,
      read_repositories: "run_scope",
      read_conversations: "none",
      write_graph_patch: false,
      write_project_files: false,
      write_paper: false,
    };
    const profile = {
      provider: "codex",
      model: "",
      reasoning: "medium",
      run_on: "local",
      permissions,
    };
    const metric = {
      bytes: 0,
      count: 0,
      limits: { max_bytes: 1, max_count: 1, ttl_seconds: 1 },
      reclaimable_bytes: 0,
      reclaimable_count: 0,
    };
    const html = renderToStaticMarkup(
      React.createElement(ProjectSettings, {
        apiBase: "/api/projects/project",
        project: {
          id: "project",
          name: "Project",
          state_repository: "repo",
          run_on: "local",
          default_run_truth_scope: ["repo"],
          repositories: [{ alias: "repo", machine: "local", path: "/repo" }],
          machines: [{ alias: "local", host: "", provider_paths: { codex: "codex" } }],
          agent_profiles: {
            seed: profile,
            refresh: { ...profile, model: "legacy-refresh" },
            node_chat: profile,
            project_chat: profile,
            paper_coach: profile,
          },
          providers: {},
          provider_readiness: {},
          cache_metrics: { remote_sources: metric, session_slices: metric },
        },
        usage: null,
        onRefreshUsage: async () => {},
        cacheClearDisabled: false,
        onSaved() {},
        onCacheMetricsChange() {},
        onRefreshReadiness: async () => {},
        showDisplaySettings: false,
        textScale: 100,
        onTextScaleChange() {},
      }),
    );

    assert.match(html, /Project boundary/);
    assert.match(html, /Agent defaults/);
    assert.equal(html.match(/<strong>Orchestrator<\/strong>/g)?.length, 1);
    assert.match(html.slice(html.indexOf("<strong>Orchestrator</strong>")), /legacy-refresh/);
    assert.doesNotMatch(html, /Your identity|Save name/);
    assert.doesNotMatch(html, /Ontology|Add node type|Add field|Add relation/);
  } finally {
    globalThis.localStorage = previousLocalStorage;
  }
});

function findElement(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const match = findElement(child, predicate);
      if (match) return match;
    }
    return null;
  }
  if (!node || typeof node !== "object") return null;
  if (node.props && predicate(node)) return node;
  const children = node.props?.children;
  for (const child of Array.isArray(children) ? children : [children]) {
    const match = findElement(child, predicate);
    if (match) return match;
  }
  return null;
}

test("the web Inbox predicate agrees with the backend over the shared fixture", () => {
  // The backend counts Decisions awaiting choice for the project card while the
  // client filters the graph itself for the Inbox, so the rule exists twice.
  // tests/test_api.py reads this same file; drift on either side fails here.
  const fixture = JSON.parse(
    readFileSync(
      new URL("../../tests/fixtures/decisions_awaiting_choice.json", import.meta.url),
      "utf8",
    ),
  );
  const nodes = Object.fromEntries(fixture.nodes.map((node) => [node.id, node]));

  assert.deepEqual(
    decisionsAwaitingChoice(Object.values(nodes), nodes).map((node) => node.id),
    fixture.expected_awaiting_choice,
  );
});
