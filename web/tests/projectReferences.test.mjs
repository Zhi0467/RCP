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
after(() => server.close());
const { buildNotificationLink, parseNotificationLink } = await server.ssrLoadModule(
  "/src/notificationLinks.ts",
);
const {
  extractReferences,
  labelArtifactReferences,
  mergeReferences,
  referenceDraftKey,
  parseReferenceDraft,
  referenceKey,
  sourceReference,
  referenceUrl,
  setReferenceDrag,
  unlabeledArtifactIds,
} = await server.ssrLoadModule("/src/projectReferences.ts");
const { conversationTurnRequest } = await server.ssrLoadModule("/src/chatWorkspace.ts");
const main = { kind: "main" };
const branch = { kind: "branch", branch_id: "branch / 雪?#" };
const ref = (id) => ({ selector: { kind: "artifact", artifact_id: id }, target: main, label: id });
const url = (projectId, kind, itemId, target = "main") =>
  buildNotificationLink({ projectId, kind, itemId, target });

test("generic reference links round trip every segment on main and branch", () => {
  for (const kind of ["artifact", "node", "paper"])
    for (const target of ["main", branch.branch_id]) {
      const link = {
        projectId: "project /?#雪",
        kind,
        itemId: kind === "paper" ? "introduction" : "id /#?雪!'().",
        target,
      };
      assert.deepEqual(parseNotificationLink(buildNotificationLink(link)), link);
      assert.deepEqual(
        parseNotificationLink(`https://rcp.test/path${buildNotificationLink(link)}`),
        link,
      );
    }
  assert.equal(parseNotificationLink(url("p", "paper", "other")), null);
});

test("paste extracts same-project links and preserves all surrounding and foreign text", () => {
  const foreign = url("other", "artifact", "a");
  const text = `before (${url("p", "node", "n", branch.branch_id)}), between https://rcp.test/${url("p", "paper", "introduction")} after ${foreign}`;
  const result = extractReferences(text, "p");
  assert.equal(result.text, `before (), between  after ${foreign}`);
  assert.deepEqual(
    result.references.map((item) => item.selector),
    [{ kind: "node", node_id: "n", branch_id: branch.branch_id }, { kind: "paper" }],
  );
  assert.equal(result.references[0].target.branch_id, branch.branch_id);
  const punctuationId = "a!'().";
  assert.equal(
    extractReferences(url("p", "artifact", punctuationId), "p").references[0].selector.artifact_id,
    punctuationId,
  );
});

test("dedupe respects node source target and shares the eight-item cap with uploads", () => {
  const nodes = [null, "branch"].map((branch_id) => ({
    selector: { kind: "node", node_id: "n", branch_id },
    target: main,
    label: "n",
  }));
  assert.equal(mergeReferences([], [...nodes, ...nodes], 0).references.length, 2);
  const result = mergeReferences([ref("a")], [ref("a"), ref("b"), ref("c")], 6);
  assert.equal(result.references.length, 2);
  assert.equal(result.rejected, 1);
  const full = extractReferences(
    `${url("p", "artifact", "a")} ${url("p", "artifact", "b")}`,
    "p",
    [ref("a")],
    7,
  );
  assert.equal(full.text, ` ${url("p", "artifact", "b")}`);
  assert.equal(full.references.length, 1);
  assert.equal(full.rejected, 1);
});

test("draft round trip is isolated by project, graph target, and chat", () => {
  const keys = [
    referenceDraftKey("p", main, "c"),
    referenceDraftKey("p", branch, "c"),
    referenceDraftKey("other", main, "c"),
    referenceDraftKey("p", main, "other"),
  ];
  assert.equal(new Set(keys).size, 4);
  const storage = new Map([[keys[0], JSON.stringify([ref("a"), ref("a")])]]);
  assert.deepEqual(
    parseReferenceDraft(storage.get(keys[0])).map((item) => referenceKey(item.selector)),
    [referenceKey(ref("a").selector)],
  );
  assert.deepEqual(parseReferenceDraft(storage.get(keys[1])), []);
  assert.deepEqual(parseReferenceDraft("broken"), []);
  assert.deepEqual(parseReferenceDraft("[{},null]"), []);
  assert.equal(
    parseReferenceDraft(JSON.stringify(Array.from({ length: 10 }, (_, i) => ref(String(i)))))
      .length,
    8,
  );
});

test("transcript sources preserve node branch and outgoing requests carry only selectors", () => {
  const item = sourceReference({
    kind: "node",
    source_id: "n",
    version: null,
    graph_head: { target: branch, revision: 3, transition_id: "t" },
  });
  assert.equal(item.selector.branch_id, branch.branch_id);
  const request = conversationTurnRequest({
    config: {},
    message: "inspect",
    kind: "project_chat",
    runTruthScope: [],
    nodeId: null,
    chatId: "c",
    sessionId: null,
    mode: "discuss",
    references: [item.selector],
  });
  assert.deepEqual(request.references, [item.selector]);
});

test("copy and drag use the same full address-bar URL and both MIME types", () => {
  const previous = globalThis.window;
  globalThis.window = { location: { href: "https://rcp.test/app?x=1#old" } };
  try {
    const selector = { kind: "node", node_id: "n /", branch_id: branch.branch_id };
    const full = referenceUrl("p", main, selector);
    assert.ok(full.startsWith("https://rcp.test/app?x=1#/"));
    assert.equal(parseNotificationLink(full).target, branch.branch_id);
    const data = new Map();
    const transfer = { setData: (kind, value) => data.set(kind, value) };
    setReferenceDrag(transfer, "p", main, selector);
    assert.equal(data.get("text/plain"), full);
    assert.equal(data.get("text/uri-list"), full);
    assert.equal(transfer.effectAllowed, "copy");
  } finally {
    globalThis.window = previous;
  }
});

test("pasted artifact chips take their saved name and unknown ids keep the fallback", () => {
  const { references } = extractReferences(
    `${url("p", "artifact", "known")} ${url("p", "artifact", "temp")}`,
    "p",
  );
  const named = { ...ref("named"), label: "Picked name" };
  const draft = [...references, named];
  assert.deepEqual(unlabeledArtifactIds(draft), ["known", "temp"]);
  const labeled = labelArtifactReferences(draft, [
    { artifact_id: "known", name: "Episode report" },
    { artifact_id: "named", name: "Renamed since" },
  ]);
  assert.equal(labeled[0].label, "Episode report");
  assert.deepEqual(unlabeledArtifactIds(labeled), ["temp"]);
  assert.equal(labeled[2], named);
  assert.equal(labelArtifactReferences(labeled, []), labeled);
});
