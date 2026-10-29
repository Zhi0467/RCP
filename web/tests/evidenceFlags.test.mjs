import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { createServer } from "vite";

test("flag rows keep open records, mark only current Evidence, and offer resolvable sources", async () => {
  const server = await createServer({
    configFile: false,
    root: new URL("..", import.meta.url).pathname,
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { evidenceFlagRows } = await server.ssrLoadModule("/src/projects/evidenceFlags.ts");
    const node = { id: "e1", type: "evidence", validity: "valid" };
    const base = { evidence_id: "e1", target: "main", state: "open", source: null };
    const repositories = [{ alias: "repo", path: "/work/repo" }];
    const rows = evidenceFlagRows(
      [
        { ...base, flag_id: "open", source: "/host/jobs/run.log" },
        { ...base, flag_id: "paper", kind: "unverified", source: "https://example.org/paper" },
        {
          ...base,
          flag_id: "addressing",
          state: "addressing",
          chat_id: "chat",
          source: "repo/log:4",
        },
        { ...base, flag_id: "added", kind: "consolidation_added" },
        { ...base, flag_id: "failed-turn", chat_id: "chat" },
        { ...base, flag_id: "dismissed", state: "dismissed" },
        { ...base, flag_id: "missing", evidence_id: "gone" },
        { ...base, flag_id: "retired", evidence_id: "e2" },
      ],
      { e1: node, e2: { ...node, id: "e2", validity: "superseded" } },
      repositories,
    );
    assert.deepEqual(
      rows.map(({ flag, node, actions }) => [flag.flag_id, node?.id ?? null, actions]),
      [
        ["open", "e1", ["address", "open_evidence", "dismiss"]],
        // An unread source is flagged only; no agent turn can read it for the human.
        ["paper", "e1", ["open_evidence", "open_source", "dismiss"]],
        ["addressing", "e1", ["open_chat"]],
        // A consolidation notice reports no problem, so there is nothing to Address.
        ["added", "e1", ["open_evidence", "dismiss"]],
        ["failed-turn", "e1", ["address", "open_chat", "open_evidence", "dismiss"]],
        ["missing", null, ["dismiss"]],
        ["retired", null, ["dismiss"]],
      ],
    );
    const flags = [
      { ...base, flag_id: "main" },
      { ...base, flag_id: "branch", target: "branch" },
    ];
    assert.deepEqual(
      evidenceFlagRows(flags, { e1: node }, repositories).map((r) => r.flag.flag_id),
      ["main"],
    );
    const branch = evidenceFlagRows(flags, { e1: node }, repositories, "branch");
    assert.deepEqual(
      branch.map((r) => r.flag.flag_id),
      ["branch"],
    );
    assert.deepEqual(branch[0].actions, ["address", "open_evidence", "dismiss"]);
    assert.deepEqual(rows[1].source, { kind: "url", href: "https://example.org/paper" });
    assert.deepEqual(rows[2].source, { kind: "file", target: { path: "/work/repo/log", line: 4 } });
  } finally {
    await server.close();
  }
});

test("Address all selects the oldest eligible findings and shares the server cap", async () => {
  const server = await createServer({
    configFile: false,
    root: new URL("..", import.meta.url).pathname,
    server: { middlewareMode: true, hmr: false },
    optimizeDeps: { noDiscovery: true },
  });
  try {
    const { evidenceFlagRows, evidenceFlagsToAddress, EVIDENCE_FLAG_ADDRESS_BATCH_MAX } =
      await server.ssrLoadModule("/src/projects/evidenceFlags.ts");
    const limits = await readFile(new URL("../../src/rcp/limits.py", import.meta.url), "utf8");
    assert.equal(
      EVIDENCE_FLAG_ADDRESS_BATCH_MAX,
      Number(limits.match(/^EVIDENCE_FLAG_ADDRESS_BATCH_MAX = (\d+)$/m)[1]),
    );
    const flags = Array.from({ length: 12 }, (_, i) => ({
      flag_id: `f-${i}`,
      evidence_id: "e",
      target: "main",
      kind: "overclaim",
      state: "open",
      created_at: new Date(Date.UTC(2026, 9, 17, 0, 12 - i)).toISOString(),
    }));
    const excluded = [
      { ...flags[0], flag_id: "notice", kind: "consolidation_added" },
      { ...flags[0], flag_id: "unread-source", kind: "unverified" },
      { ...flags[0], flag_id: "busy", state: "addressing" },
      { ...flags[0], flag_id: "missing", evidence_id: "gone" },
      { ...flags[0], flag_id: "retired", evidence_id: "old" },
    ];
    const rows = evidenceFlagRows(
      [...flags, ...excluded],
      {
        e: { id: "e", type: "evidence", validity: "valid" },
        old: { id: "old", type: "evidence", validity: "superseded" },
      },
      [],
    );
    const selectedIds = evidenceFlagsToAddress(rows).map((flag) => flag.flag_id);
    const { addressEvidenceFlags } = await server.ssrLoadModule("/src/core/api.ts");
    const originalFetch = globalThis.fetch;
    const requests = [];
    try {
      globalThis.fetch = async (url, init) => {
        requests.push({ url, method: init.method, body: JSON.parse(init.body) });
        return Response.json({ flags: [], chat_id: "shared", operation_id: "turn" });
      };
      await addressEvidenceFlags("/api/projects/demo", selectedIds, "batch-key");
    } finally {
      globalThis.fetch = originalFetch;
    }
    assert.deepEqual(requests, [
      {
        url: "/api/projects/demo/consolidation/evidence-flags/address",
        method: "POST",
        body: { request_id: "batch-key", flag_ids: selectedIds },
      },
    ]);
    assert.deepEqual(
      selectedIds,
      [...flags]
        .reverse()
        .slice(0, 10)
        .map((flag) => flag.flag_id),
    );
  } finally {
    await server.close();
  }
});
