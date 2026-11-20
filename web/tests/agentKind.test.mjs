import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const WEB = fileURLToPath(new URL("..", import.meta.url));
const SOURCE = join(WEB, "src");
const BASELINE = fileURLToPath(new URL("./fixtures/agentKindBaseline.json", import.meta.url));

const ALLOWLIST = new Set(["core/types.ts", "agentWindow/agentWindowEndpoints.ts"]);
const AGENT_KINDS = new Set([
  "space_chat",
  "node_chat",
  "project_chat",
  "auto_research",
  "experiment_loop",
]);
const QUOTED_NAME = /(["'`])([a-z_]+)\1/g;
const THREAD_KIND_COMPARISON = /\bkind\s*[!=]==\s*(["'`])(?:space|project)\1/g;

function sourceFiles(directory) {
  return readdirSync(directory)
    .sort()
    .flatMap((name) => {
      const path = join(directory, name);
      if (statSync(path).isDirectory()) return sourceFiles(path);
      return /\.(ts|tsx)$/.test(name) ? [path] : [];
    });
}

function agentKindCount(text) {
  let count = [...text.matchAll(THREAD_KIND_COMPARISON)].length;
  for (const match of text.matchAll(QUOTED_NAME)) {
    if (AGENT_KINDS.has(match[2])) count += 1;
  }
  return count;
}

function moduleCounts() {
  const counts = {};
  for (const path of sourceFiles(SOURCE)) {
    const module = relative(SOURCE, path).split("\\").join("/");
    if (ALLOWLIST.has(module)) continue;
    const count = agentKindCount(readFileSync(path, "utf8"));
    if (count) counts[module] = count;
  }
  return counts;
}

test("the counter sees each form", () => {
  assert.equal(agentKindCount('if (task.kind === "project_chat") {}'), 1);
  assert.equal(agentKindCount("thread.kind !== 'space'"), 1);
  assert.equal(agentKindCount('shape.kind === "box" || name === "space"'), 0);
});

test("allowlisted modules exist", () => {
  const missing = [...ALLOWLIST].filter((module) => {
    try {
      return !statSync(join(SOURCE, module)).isFile();
    } catch {
      return true;
    }
  });
  assert.deepEqual(missing, [], `agent-kind owner modules missing: ${missing}`);
});

test("agent-kind coupling only falls", () => {
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
    `These modules name more agent kinds than before (baseline, now): ${JSON.stringify(grown)}.`,
  );
  const fallen = Object.fromEntries(
    Object.entries(baseline)
      .filter(([module, count]) => (current[module] ?? 0) < count)
      .map(([module, count]) => [module, [count, current[module] ?? 0]]),
  );
  assert.deepEqual(
    fallen,
    {},
    `Agent-kind coupling fell (baseline, now): ${JSON.stringify(fallen)}. Lock in the gain ` +
      "with `node --experimental-strip-types web/tests/agentKind.test.mjs --write-baseline`.",
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
