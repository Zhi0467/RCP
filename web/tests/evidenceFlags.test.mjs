import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "vite";

test("flag rows expose state actions and omit absent, retired, and dismissed records", async () => {
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
    const rows = evidenceFlagRows(
      [
        { ...base, flag_id: "open" },
        {
          ...base,
          flag_id: "addressing",
          state: "addressing",
          chat_id: "chat",
          source: "repo/log:4",
        },
        { ...base, flag_id: "dismissed", state: "dismissed" },
        { ...base, flag_id: "missing", evidence_id: "gone" },
        { ...base, flag_id: "retired", evidence_id: "e2" },
      ],
      { e1: node, e2: { ...node, id: "e2", validity: "superseded" } },
    );
    assert.equal(rows.length, 2);
    assert.deepEqual(
      rows.map(({ flag }) => [flag.flag_id, flag.state]),
      [
        ["open", "open"],
        ["addressing", "addressing"],
      ],
    );
    assert.deepEqual(
      rows.map(({ actions }) => actions),
      [
        ["address", "open_evidence", "dismiss"],
        ["open_chat", "open_evidence", "open_source", "dismiss"],
      ],
    );
  } finally {
    await server.close();
  }
});
