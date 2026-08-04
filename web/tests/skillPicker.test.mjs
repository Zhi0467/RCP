import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const {
  addSkillSelection,
  clearSkillTrigger,
  filterSkillCatalog,
  hasSkillSelection,
  moveSkillHighlight,
  readSkillTrigger,
  removeSkillSelection,
  selectedSkillRefs,
} = await server.ssrLoadModule("/src/skillPicker.ts");

after(() => server.close());

const catalog = [
  {
    id: "research-graph-audit",
    kind: "workflow",
    label: "Research graph audit",
    version: "1.0.0",
    description: "Review graph structure.",
    dependencies: [],
  },
  {
    id: "graph-audit",
    kind: "skill",
    label: "Graph audit",
    version: "1.0.0",
    description: "Inspect the project graph.",
    dependencies: [],
  },
];

test("a trigger opens only at the end of a word boundary", () => {
  assert.equal(readSkillTrigger("/"), "");
  assert.equal(readSkillTrigger("$gra"), "gra");
  assert.equal(readSkillTrigger("check this /graph"), "graph");
  assert.equal(readSkillTrigger("look at src/rcp/runs"), null);
  assert.equal(readSkillTrigger("/graph then more words"), null);
  assert.equal(readSkillTrigger(""), null);
});

test("choosing an entry removes only the trigger word", () => {
  assert.equal(clearSkillTrigger("audit the graph /gra"), "audit the graph ");
  assert.equal(clearSkillTrigger("$"), "");
});

test("the dropdown filters on id, label, and kind", () => {
  assert.deepEqual(
    filterSkillCatalog(catalog, "").map((item) => item.id),
    ["research-graph-audit", "graph-audit"],
  );
  assert.deepEqual(
    filterSkillCatalog(catalog, "workflow").map((item) => item.id),
    ["research-graph-audit"],
  );
  // Label matching is case-insensitive.
  assert.deepEqual(
    filterSkillCatalog(catalog, "Graph Audit").map((item) => item.id),
    ["research-graph-audit", "graph-audit"],
  );
  assert.deepEqual(
    filterSkillCatalog(catalog, "nothing").map((item) => item.id),
    [],
  );
  assert.deepEqual(
    filterSkillCatalog(catalog, "graph-audit").map((item) => item.id),
    ["research-graph-audit", "graph-audit"],
  );
});

test("arrow keys wrap around both ends of the dropdown", () => {
  assert.equal(moveSkillHighlight(0, 2, 1), 1);
  assert.equal(moveSkillHighlight(1, 2, 1), 0);
  assert.equal(moveSkillHighlight(0, 2, -1), 1);
  assert.equal(moveSkillHighlight(0, 0, 1), 0);
});

test("selection is structured by kind and never duplicates an entry", () => {
  let selection = { workflow_ids: [], skill_ids: [] };
  assert.equal(hasSkillSelection(selection), false);

  selection = addSkillSelection(selection, catalog[0]);
  selection = addSkillSelection(selection, catalog[1]);
  selection = addSkillSelection(selection, catalog[1]);

  assert.deepEqual(selection, {
    workflow_ids: ["research-graph-audit"],
    skill_ids: ["graph-audit"],
  });
  assert.deepEqual(selectedSkillRefs(selection), [
    ["workflow", "research-graph-audit"],
    ["skill", "graph-audit"],
  ]);

  selection = removeSkillSelection(selection, "workflow", "research-graph-audit");
  assert.deepEqual(selection, { workflow_ids: [], skill_ids: ["graph-audit"] });
});
