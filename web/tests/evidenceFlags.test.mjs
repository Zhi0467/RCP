import assert from "node:assert/strict";
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
    const base = { evidence_id: "e1", state: "open", source: null };
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
        ["paper", "e1", ["address", "open_evidence", "open_source", "dismiss"]],
        ["addressing", "e1", ["open_chat"]],
        // A consolidation notice reports no problem, so there is nothing to Address.
        ["added", "e1", ["open_evidence", "dismiss"]],
        ["failed-turn", "e1", ["address", "open_chat", "open_evidence", "dismiss"]],
        ["missing", null, ["dismiss"]],
        ["retired", null, ["dismiss"]],
      ],
    );
    // Off main the open graph cannot rule out Address; main's endpoint decides.
    const branch = evidenceFlagRows([{ ...base, flag_id: "new-on-main" }], {}, repositories, false);
    assert.deepEqual(branch[0].actions, ["address", "dismiss"]);
    assert.deepEqual(rows[1].source, { kind: "url", href: "https://example.org/paper" });
    assert.deepEqual(rows[2].source, { kind: "file", target: { path: "/work/repo/log", line: 4 } });
  } finally {
    await server.close();
  }
});
