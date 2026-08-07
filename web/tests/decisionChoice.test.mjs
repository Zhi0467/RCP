import assert from "node:assert/strict";
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
const { DetailDrawer } = await server.ssrLoadModule("/src/components/DetailDrawer.tsx");
const { presentNode } = await server.ssrLoadModule("/src/nodePresentation.ts");

after(() => server.close());

const decision = {
  id: "dec/resource",
  type: "decision",
  title: "Choose resource level",
  question: "Which resource level should the experiment use?",
  options: ["Small", "Medium", "Medium", "Large"],
  selected_option: "Medium",
  status: "decided",
  rationale: "Balance iteration speed against signal quality.",
  consequences: "This choice governs the next experiment.",
  standing: "accepted",
  created_rev: 2,
  updated_rev: 4,
  source_refs: [],
  extension_fields: {},
  draft_touched: true,
};

const commonProps = {
  node: decision,
  edges: [],
  allNodes: { [decision.id]: decision },
  glossaryIndex: { entriesByInitial: new Map() },
  beliefTransitions: [],
  validationMessages: [],
  ontology: { types: [], fields: [], relations: [] },
  onClose() {},
  onDock() {},
  onBeginEdit() {},
  onStanding() {},
  onStage() {},
  onDecisionChoice() {},
  onOpenChat() {},
  onExploreRelations() {},
  onSelectNode() {},
};

function renderDrawer(props = {}) {
  const previousWindow = globalThis.window;
  globalThis.window = { innerWidth: 1440, innerHeight: 900 };
  try {
    return renderToStaticMarkup(React.createElement(DetailDrawer, { ...commonProps, ...props }));
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
}

test("Decision detail renders one accessible staged ballot above Context", () => {
  const html = renderDrawer({
    pendingDecisionProposalCount: 2,
    decisionChoiceStaged: true,
  });

  assert.match(html, /<section class="decision-choice-section">/);
  assert.match(html, /<span class="decision-choice-status decided">Decided · staged<\/span>/);
  assert.match(
    html,
    /<legend id="decision-question-dec\/resource">Which resource level should the experiment use\?<\/legend>/,
  );
  assert.equal(html.match(/type="radio"/g)?.length, 3);
  assert.equal(html.match(/checked=""/g)?.length, 1);
  assert.match(
    html,
    /class="decision-choice-option selected staged"[\s\S]*value="Medium"[\s\S]*Staged selection/,
  );
  assert.match(html, /2 pending proposals target this decision/);
  assert.ok(html.indexOf("decision-choice-section") < html.indexOf("node-context"));
  assert.equal(html.match(/>Medium</g)?.length, 1);
  assert.doesNotMatch(html, /Options considered|Selected option/);

  const contextKeys = presentNode(decision).context.map((item) => item.key);
  assert.deepEqual(contextKeys, ["rationale", "consequences"]);
});

test("Decision choices disable for superseded, globally disabled, and removal-staged records", () => {
  for (const props of [
    { node: { ...decision, status: "superseded" } },
    { mutationsDisabled: true },
    { stagedForRemoval: true },
  ]) {
    const html = renderDrawer(props);
    assert.match(html, /<fieldset disabled="">/);
  }
});
