import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { execFileSync } from "node:child_process";
import test from "node:test";

const script = readFileSync(new URL("../../src/rcp/artifact_live.js", import.meta.url), "utf8");
function shell() {
  const delivered = [],
    forwarded = [],
    requests = [],
    listeners = {};
  const frame = {
    dataset: {},
    style: {},
    contentWindow: { postMessage: (value) => delivered.push(value) },
  };
  const window = {
    location: { href: "https://rcp.test/viewer", origin: "https://rcp.test" },
    parent: { postMessage: (...args) => forwarded.push(args) },
    addEventListener: (kind, listener) => (listeners[kind] = listener),
  };
  const context = vm.createContext({
    window,
    URL,
    Set,
    liveArtifactId: "trusted",
    liveUrl: "/live",
    defaultLiveDelay: 2000,
    clearTimeout() {},
    setTimeout() {},
    document: {
      hidden: false,
      getElementById: (id) => (id === "preview" ? frame : null),
      addEventListener() {},
    },
    fetch: async (url) => {
      requests.push(url);
      return { ok: true, json: async () => context.reply };
    },
  });
  vm.runInContext(script, context);
  return {
    context,
    frame,
    delivered,
    forwarded,
    requests,
    listeners,
    refresh: async (reply) => {
      context.reply = reply;
      await vm.runInContext("liveReady=true; refreshLive()", context);
    },
  };
}
const full = (head) => ({ kind: "graph", head, nodes: [{ id: "node" }], edges: [] });
const message = (graph) => ({
  kind: "rcp-live-data",
  version: 1,
  complete: !graph.error,
  refresh_seconds: 2,
  snapshots: [graph],
});

test("only complete graph caches advance heads; errors retain polling and stale mark until recovery", async () => {
  const s = shell();
  await s.refresh(message(full("one")));
  assert.equal(s.requests[0].searchParams.has("head"), false);
  await s.refresh(message({ kind: "graph", error: "graph_too_large" }));
  assert.equal(s.frame.dataset.graphStale, "true");
  assert.equal(vm.runInContext("liveFinished", s.context), false);
  assert.equal(s.requests.at(-1).searchParams.get("head"), "one");
  await s.refresh(message({ kind: "graph", head: "one", unchanged: true }));
  assert.equal(s.delivered.at(-1).snapshots[0].nodes[0].id, "node");
  assert.equal(s.frame.dataset.graphStale, "false");
  await s.refresh(message(full("two")));
  await s.refresh(message({ kind: "graph", error: "message_too_large" }));
  assert.equal(s.requests.at(-1).searchParams.get("head"), "two");
  const fresh = shell();
  await fresh.refresh(message({ kind: "graph", head: "two", unchanged: true }));
  assert.equal(fresh.requests[0].searchParams.has("head"), false);
  assert.equal(fresh.delivered.length, 0);
});

test("shell accepts only its child and cached ids, supplies trusted identity and exact app origin", async () => {
  const s = shell();
  await s.refresh(message(full("head")));
  const send = (source, data) => s.listeners.message({ source, data });
  const payload = { type: "rcp-open-node", version: 1, id: "node", artifact_id: "forged" };
  send({}, payload);
  send(s.frame.contentWindow, { ...payload, id: "missing" });
  send(s.frame.contentWindow, { ...payload, version: 2 });
  assert.equal(s.forwarded.length, 0);
  send(s.frame.contentWindow, payload);
  assert.deepEqual(JSON.parse(JSON.stringify(s.forwarded)), [
    [
      { type: "rcp-open-node", version: 1, artifact_id: "trusted", node_id: "node" },
      "https://rcp.test",
    ],
  ]);
  await s.refresh(message({ ...full("next"), nodes: [] }));
  send(s.frame.contentWindow, payload);
  assert.equal(s.forwarded.length, 1);
});

test("content wrapper forwards only its own page and ids from delivered graph data", () => {
  const script = execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      "from rcp.artifact_views import live_frame_addon; print(live_frame_addon().wrapper_script)",
    ],
    { encoding: "utf8" },
  );
  const listeners = [],
    forwarded = [],
    child = {},
    parent = {};
  const window = { parent };
  vm.runInNewContext(script, {
    window,
    Set,
    artifact: () => ({ contentWindow: child }),
    listen: (_target, _kind, listener) => listeners.push(listener),
    parentPost: (value) => forwarded.push(value),
    channelReady: [],
    artifactPort: {},
    portPost() {},
  });
  const send = (source, data) => listeners.forEach((fn) => fn({ source, data }));
  const payload = { type: "rcp-open-node", version: 1, id: "node" };
  send(child, payload);
  send(parent, message(full("h")));
  send(parent, payload);
  send({}, payload);
  send(child, { ...payload, id: "missing" });
  assert.equal(forwarded.length, 0);
  send(child, payload);
  assert.deepEqual(JSON.parse(JSON.stringify(forwarded)), [payload]);
  send(parent, message({ ...full("next"), nodes: [] }));
  send(child, payload);
  assert.equal(forwarded.length, 1);
});
