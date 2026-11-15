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
const { copyShareLink, shareLinkGraphTarget, shareLinkRequest, shareLinksEnabled } =
  await server.ssrLoadModule("/src/core/shareLinks.ts");
const { ApiError } = await server.ssrLoadModule("/src/core/api.ts");

class FakeItem {
  constructor(parts) {
    this.parts = parts;
  }
}

function fakeClipboard() {
  const written = [];
  return {
    written,
    write: async (items) => {
      for (const item of items) written.push(await (await item.parts["text/plain"]).text());
    },
  };
}

function respond(status, body) {
  return async () => {
    if (status === 200) return body;
    throw new ApiError(body?.detail ?? "Request failed", status);
  };
}

test("graph targets and references map to share-link requests", () => {
  assert.equal(shareLinkGraphTarget({ kind: "main" }), "main");
  assert.equal(shareLinkGraphTarget({ kind: "branch", branch_id: "b1" }), "branch:b1");
  assert.deepEqual(
    shareLinkRequest({ kind: "branch", branch_id: "b1" }, { kind: "artifact", artifact_id: "a1" }),
    { kind: "artifact", item_id: "a1", graph_target: "branch:b1" },
  );
  assert.deepEqual(
    shareLinkRequest(
      { kind: "branch", branch_id: "b1" },
      { kind: "node", node_id: "n1", branch_id: null },
    ),
    { kind: "node", item_id: "n1", graph_target: "main" },
  );
  assert.deepEqual(
    shareLinkRequest({ kind: "main" }, { kind: "node", node_id: "n1", branch_id: "b2" }),
    { kind: "node", item_id: "n1", graph_target: "branch:b2" },
  );
});

test("copy posts the item and writes the returned URL inside the click", async () => {
  const calls = [];
  const clipboard = fakeClipboard();
  const post = async (path, init) => {
    calls.push({ path, init });
    return { url: "https://lab.example/open#l=t1" };
  };
  const copied = copyShareLink(
    "p 1",
    { kind: "chat", item_id: "c1", graph_target: "main" },
    { post, clipboard, Item: FakeItem },
  );
  assert.equal(calls.length, 1);
  await copied;
  assert.equal(calls[0].path, "/api/projects/p%201/share-links");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    kind: "chat",
    item_id: "c1",
    graph_target: "main",
  });
  assert.deepEqual(clipboard.written, ["https://lab.example/open#l=t1"]);
});

test("a refused link rejects with the server's reason and writes nothing", async () => {
  for (const [status, body] of [
    [409, { detail: "This team space has no access address." }],
    [404, { detail: "Not found" }],
    [500, null],
    [503, { detail: "Team state is briefly unavailable." }],
  ]) {
    const clipboard = fakeClipboard();
    await assert.rejects(
      copyShareLink(
        "p",
        { kind: "node", item_id: "n", graph_target: "main" },
        { post: respond(status, body), clipboard, Item: FakeItem },
      ),
      (error) => error instanceof Error && error.message.length > 0,
    );
    assert.deepEqual(clipboard.written, []);
  }
});

test("a blocked clipboard rejects", async () => {
  await assert.rejects(
    copyShareLink(
      "p",
      { kind: "node", item_id: "n", graph_target: "main" },
      {
        post: respond(200, { url: "https://lab.example/open#l=t" }),
        clipboard: { write: async () => Promise.reject(new Error("denied")) },
        Item: FakeItem,
      },
    ),
  );
});

test("share links are on only in a team space with an access URL", async () => {
  const space = (kind, status, body) => {
    const paths = [];
    const fetcher = async (path) => {
      paths.push(path);
      return path === "/api/health"
        ? new Response(JSON.stringify({ space_kind: kind }), { status: 200 })
        : new Response(JSON.stringify(body), { status });
    };
    return { paths, fetcher };
  };
  const linked = space("team", 200, { space_name: "Lab", access_url: "https://lab.example" });
  assert.equal(await shareLinksEnabled(linked.fetcher), true);
  assert.equal(
    await shareLinksEnabled(space("team", 200, { space_name: "Lab", access_url: null }).fetcher),
    false,
  );
  assert.equal(await shareLinksEnabled(space("team", 404, { detail: "Not found" }).fetcher), false);
  const personal = space("personal", 404, { detail: "Not found" });
  assert.equal(await shareLinksEnabled(personal.fetcher), false);
  assert.deepEqual(personal.paths, ["/api/health"]);
});
