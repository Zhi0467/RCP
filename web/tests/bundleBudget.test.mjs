import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import { gzipSync } from "node:zlib";

const dist = new URL("../dist/", import.meta.url);
const manifestUrl = new URL(".vite/manifest.json", dist);
const budget = JSON.parse(readFileSync(new URL("../bundle-budget.json", import.meta.url), "utf8"));

/** Gzip bytes of one built file, named by its path under dist. */
const gzipBytes = (file) => gzipSync(readFileSync(new URL(file, dist))).length;

/**
 * Measures the current build from Vite's manifest, which lists only this build's
 * chunks; `dist/assets` keeps stale ones because the build never empties it.
 * Each chunk is labelled by its stable name, and its CSS by that name plus " css".
 */
function measure(manifest) {
  const chunks = new Map();
  for (const [key, chunk] of Object.entries(manifest)) {
    if (!chunk.file.endsWith(".js")) continue;
    const label = chunk.name ?? key;
    assert.ok(!chunks.has(label), `Two chunks share the name ${label}`);
    chunks.set(label, gzipBytes(chunk.file));
    const css = (chunk.css ?? []).reduce((total, file) => total + gzipBytes(file), 0);
    if (css) chunks.set(`${label} css`, css);
  }
  const initial = new Set();
  const visit = (key) => {
    const chunk = manifest[key];
    for (const file of [chunk.file, ...(chunk.css ?? [])]) initial.add(file);
    for (const imported of chunk.imports ?? []) visit(imported);
  };
  for (const [key, chunk] of Object.entries(manifest)) if (chunk.isEntry) visit(key);
  const initialBytes = [...initial].reduce((total, file) => total + gzipBytes(file), 0);
  return { chunks, initialBytes };
}

test("the built bundle stays within its gzip budget", () => {
  assert.ok(existsSync(manifestUrl), "Run `npm run build` first.");
  const { chunks, initialBytes } = measure(JSON.parse(readFileSync(manifestUrl, "utf8")));
  const over = [];
  for (const [label, ceiling] of Object.entries(budget.chunks)) {
    assert.ok(chunks.has(label), `No built chunk is named ${label}; rename its budget entry.`);
    if (chunks.get(label) > ceiling) over.push(`${label}: ${chunks.get(label)} > ${ceiling}`);
  }
  if (initialBytes > budget.initial) over.push(`initial load: ${initialBytes} > ${budget.initial}`);
  assert.deepEqual(over, []);
});
