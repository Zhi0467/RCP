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
  "types.ts",
  "graph/researchType.ts",
  "graph/researchProjection.ts",
