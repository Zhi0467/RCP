// Node-type knowledge stays in the Web research layer.
//
// The Web mirror of tests/test_project_types.py. Generic modules ask the
// project type in src/researchType.ts which node types play which part; only
// the research layer below names research node types or research relations.
//
// The ratchet counts, per other module under web/src, quoted string literals
// ("…", '…', or `…`) whose whole text is a research node type or relation name.
// It is a plain regex over source text, so a quoted name in a comment counts
// too. A count may fall, never rise. What remains are persisted or wire names,
// such as the "decision" and "blocker" notification kinds or the "experiment"
// timeline actor kind. After lowering a count, lock it in with:
//
//   node --experimental-strip-types web/tests/projectType.test.mjs --write-baseline

import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BASE_TYPE_PRESENTATION,
  EDIT_FIELDS_BY_TYPE,
  FLOW_ORDER,
  RESEARCH,
  STAGE_BY_TYPE,
  TYPE_LENS,
  editFieldsFor,
  isBelief,
  isBeliefOutcomeRelation,
  isBlocker,
  isBlockingRelation,
  isChooser,
  isControlNode,
  isOutcome,
  isProtectedBelief,
  isQuestion,
  lifecycleField,
  orderedTypes,
  primaryField,
  typeLabel,
} from "../src/researchType.ts";

const WEB = fileURLToPath(new URL("..", import.meta.url));
const SOURCE = join(WEB, "src");
const BACKEND_TYPE = fileURLToPath(new URL("../../src/rcp/core/research_type.py", import.meta.url));
const BACKEND_MODELS = fileURLToPath(new URL("../../src/rcp/core/models.py", import.meta.url));
const BASELINE = fileURLToPath(
  new URL("./fixtures/nodeTypeCouplingBaseline.json", import.meta.url),
);

// The research layer: the research type and wire-model mirror, and the
// research-only features (Research paths, the Experiment board, Auto-research,
// the paper). Another project type would replace these modules, not edit them.
const RESEARCH_LAYER = new Set([
  "core/types.ts",
  "graph/researchType.ts",
  "graph/researchProjection.ts",
  "experiments/experimentBoard.ts",
  "core/experimentGuidance.ts",
  "experiments/ExperimentBoard.tsx",
  "experiments/ExperimentRunDetail.tsx",
  "experiments/AutoResearchDialog.tsx",
  "paper/PaperWorkspace.tsx",
]);
// Meta relations such as `supersedes` belong to every graph, not to research.
const META_RELATIONS = new Set(["supersedes", "duplicate_of"]);
const RESEARCH_NAMES = new Set([...RESEARCH.nodeTypes, ...RESEARCH.relations]);
const QUOTED_NAME = /(["'`])([a-z_]+)\1/g;

function sourceFiles(directory) {
  return readdirSync(directory)
    .sort()
    .flatMap((name) => {
      const path = join(directory, name);
      if (statSync(path).isDirectory()) return sourceFiles(path);
      return /\.(ts|tsx)$/.test(name) ? [path] : [];
    });
}

function moduleCounts() {
  const counts = {};
  for (const path of sourceFiles(SOURCE)) {
    const module = relative(SOURCE, path).split("\\").join("/");
    if (RESEARCH_LAYER.has(module)) continue;
    let count = 0;
    for (const match of readFileSync(path, "utf8").matchAll(QUOTED_NAME)) {
      if (RESEARCH_NAMES.has(match[2])) count += 1;
    }
    if (count) counts[module] = count;
  }
  return counts;
}

function pythonStrings(text) {
  return [...text.matchAll(/"([^"]*)"/g)].map((match) => match[1]);
}

function pythonBlock(source, pattern, what) {
  const match = source.match(pattern);
  assert.ok(match, `could not find ${what} in the backend source`);
  return match[1];
}

function pythonMapping(source, name) {
  const body = pythonBlock(source, new RegExp(`\\b${name}=\\{([^}]*)\\}`), name);
  return Object.fromEntries(
    [...body.matchAll(/"([^"]*)":\s*"([^"]*)"/g)].map((match) => [match[1], match[2]]),
  );
}

const camel = (name) => name.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());

test("the Web research type mirrors the backend research type", () => {
  const backend = readFileSync(BACKEND_TYPE, "utf8");
  assert.deepEqual(
    RESEARCH.nodeTypes,
    pythonStrings(pythonBlock(backend, /\bnode_types=\(([^)]*)\)/, "node_types")),
  );
  assert.deepEqual(RESEARCH.labels, pythonMapping(backend, "labels"));
  assert.deepEqual(RESEARCH.pluralLabels, pythonMapping(backend, "plural_labels"));
  assert.deepEqual(RESEARCH.lifecycleFields, pythonMapping(backend, "lifecycle_fields"));
  const roles = [...backend.matchAll(/\b(\w+)=frozenset\(\s*\{([^}]*)\}\s*\)/g)];
  assert.ok(roles.length >= 10, "expected the backend role sets");
  for (const [, name, body] of roles) {
    const mirrored = RESEARCH[camel(name)];
    assert.ok(mirrored instanceof Set, `researchType.ts has no ${camel(name)} for ${name}`);
    assert.deepEqual([...mirrored].sort(), pythonStrings(body).sort(), name);
  }

  const models = readFileSync(BACKEND_MODELS, "utf8");
  const relations = pythonStrings(
    pythonBlock(models, /\bBaseRelation = Literal\[([^\]]*)\]/, "BaseRelation"),
  ).filter((relation) => !META_RELATIONS.has(relation));
  assert.deepEqual([...RESEARCH.relations].sort(), relations.sort());
});

test("role predicates answer from the research type", () => {
  const predicates = [
    [isProtectedBelief, RESEARCH.protectedBeliefTypes],
    [isBelief, RESEARCH.beliefTypes],
    [isOutcome, RESEARCH.outcomeTypes],
    [isControlNode, RESEARCH.controlNodeTypes],
    [isChooser, RESEARCH.chooserTypes],
    [isBlocker, RESEARCH.blockerTypes],
    [isQuestion, RESEARCH.questionTypes],
  ];
  for (const [predicate, role] of predicates) {
    assert.ok(role.size > 0);
    for (const type of RESEARCH.nodeTypes) assert.equal(predicate(type), role.has(type), type);
    assert.equal(predicate(undefined), false);
    assert.equal(predicate(null), false);
    assert.equal(predicate("custom_type"), false);
  }
  for (const relation of RESEARCH.relations) {
    assert.equal(isBeliefOutcomeRelation(relation), RESEARCH.beliefOutcomeRelations.has(relation));
    assert.equal(isBlockingRelation(relation), RESEARCH.blockingRelations.has(relation));
  }
  for (const type of RESEARCH.nodeTypes) {
    assert.equal(lifecycleField(type), RESEARCH.lifecycleFields[type] ?? "status");
    assert.equal(typeof typeLabel(type), "string");
    assert.equal(typeof typeLabel(type, true), "string");
  }
  assert.deepEqual(orderedTypes([...RESEARCH.nodeTypes].reverse()), [...RESEARCH.nodeTypes]);
});

test("presentation tables cover every node type", () => {
  const types = [...RESEARCH.nodeTypes];
  assert.deepEqual(
    BASE_TYPE_PRESENTATION.map((item) => item.name),
    types,
  );
  for (const table of [EDIT_FIELDS_BY_TYPE, TYPE_LENS, STAGE_BY_TYPE]) {
    assert.deepEqual(Object.keys(table).sort(), [...types].sort());
  }
  assert.deepEqual([...FLOW_ORDER].sort(), [...types].sort());
  for (const item of BASE_TYPE_PRESENTATION) {
    assert.equal(primaryField(item.name), item.primaryField);
    assert.deepEqual(editFieldsFor({ type: item.name, status: "open" })[0].key, "title");
  }
});

test("research-layer modules exist", () => {
  const missing = [...RESEARCH_LAYER].filter((module) => {
    try {
      return !statSync(join(SOURCE, module)).isFile();
    } catch {
      return true;
    }
  });
  assert.deepEqual(missing, [], `research-layer modules missing: ${missing}`);
});

test("node-type coupling only falls", () => {
  const baseline = JSON.parse(readFileSync(BASELINE, "utf8"));
  const current = moduleCounts();
  const grown = Object.fromEntries(
    Object.entries(current)
      .filter(([module, count]) => count > (baseline[module] ?? 0))
      .map(([module, count]) => [module, [baseline[module] ?? 0, count]]),
  );
  assert.deepEqual(
    grown,
    {},
    "These modules name more research node types or relations than before " +
      `(baseline, now): ${JSON.stringify(grown)}. Ask the predicates and tables in ` +
      "src/researchType.ts instead, or move a research-only feature into the research layer.",
  );
  const fallen = Object.fromEntries(
    Object.entries(baseline)
      .filter(([module, count]) => (current[module] ?? 0) < count)
      .map(([module, count]) => [module, [count, current[module] ?? 0]]),
  );
  assert.deepEqual(
    fallen,
    {},
    `Node-type coupling fell (baseline, now): ${JSON.stringify(fallen)}. Lock in the gain ` +
      "with `node --experimental-strip-types web/tests/projectType.test.mjs --write-baseline`.",
  );
});

if (process.argv.includes("--write-baseline")) {
  const counts = moduleCounts();
  const sorted = Object.fromEntries(
    Object.keys(counts)
      .sort()
      .map((key) => [key, counts[key]]),
  );
  writeFileSync(BASELINE, `${JSON.stringify(sorted, null, 2)}\n`);
}
