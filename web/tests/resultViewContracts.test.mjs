import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/types.ts", import.meta.url), "utf8");

function interfaceFields(name) {
  const match = source.match(new RegExp(`export interface ${name} \\{([\\s\\S]*?)\\n\\}`));
  assert.ok(match, `${name} must be exported`);
  return match[1]
    .trim()
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

test("result view requests mirror the strict create or revise union", () => {
  assert.match(
    source,
    /export type ResultViewRequest = \{ action: "create" \} \| \{ action: "revise"; view_id: string \};/,
  );
  assert.match(source, /result_view\?: ResultViewRequest \| null;/);
});

test("the public result view descriptor contains no private binding metadata", () => {
  assert.deepEqual(interfaceFields("ResultViewDescriptor"), [
    "view_id: string;",
    "chat_id: string;",
    "experiment_id: string;",
    "name: string;",
    'media_type: "text/html";',
    'state: "temporary" | "kept";',
    "created_at: string;",
    "updated_at: string;",
    "expires_at: string;",
    "kept_filename: string | null;",
    "kept_at: string | null;",
    "can_revise: boolean;",
  ]);
});
