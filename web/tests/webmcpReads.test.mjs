import assert from "node:assert/strict";
import { after, test } from "node:test";
import { execFileSync } from "node:child_process";
import { createServer } from "vite";
import { createIdentityGate, createVoiceExecutor } from "../src/voice/voiceExecutor.ts";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const {
  admittedReadRoutes,
  buildReadUrl,
  projectBroadReadToolDefinitions,
  readBoundedResponse,
  PROJECT_READ_POLICY,
  READ_LIMITS,
} = await server.ssrLoadModule("/src/webmcp/reads.ts");
const { apiReadResponse, registerAccessLossHandler, registerTransportFailureHandler } =
  await server.ssrLoadModule("/src/core/api.ts");
const { catalog, catalogAsFunctionTools, webMcpHostDefinitions } = await server.ssrLoadModule(
  "/src/voice/toolCatalog.ts",
);
after(() => server.close());
const schema = JSON.parse(
  execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      `
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from rcp.api.app import create_app
with TemporaryDirectory() as directory:
    print(json.dumps(create_app(data_dir=Path(directory)).openapi()))
`,
    ],
    { cwd: new URL("../..", import.meta.url), maxBuffer: READ_LIMITS.schemaBytes },
  ).toString(),
);
const prefix = PROJECT_READ_POLICY.prefix;
const project = { id: "p", graph_target: { kind: "branch", branch_id: "b" } };
const origin = "http://localhost:8457";
const result = (value) => JSON.parse(value.content[0].text);
const response = (body = "{}", mime = "application/json", init = {}) =>
  new Response(body, {
    ...init,
    headers: { "content-type": mime, ...init.headers },
  });
function harness(t, reply = () => response(), document = schema) {
  const window = Object.assign(new EventTarget(), { location: { origin } });
  t.mock.property(globalThis, "window", window);
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, init });
    return new URL(url).pathname === "/openapi.json"
      ? response(JSON.stringify(document))
      : reply(url, init);
  });
  const owner = new AbortController();
  const [list, read] = projectBroadReadToolDefinitions(project, () => owner.signal);
  return { list, read, owner, calls, window };
}

// Node does not normally define window; mock.property needs an existing property.
globalThis.window = undefined;
after(() => {
  delete globalThis.window;
  registerAccessLossHandler(null);
});

async function pages(tool, input = {}) {
  let text = "",
    offset = 0;
  do {
    const raw = await tool.execute({ ...input, offset });
    assert.ok(raw.content[0].text.length <= READ_LIMITS.resultChars);
    const page = result(raw);
    text += page.data;
    assert.equal(page.truncated, page.next_offset !== null);
    if (page.next_offset !== null) assert.ok(page.next_offset > offset);
    offset = page.next_offset;
    if (offset === null) assert.equal(text.length, page.total_chars);
  } while (offset !== null);
  return text;
}

function inputFor(route) {
  const input = { route: route.template, params: {}, query: {} };
  for (const p of route.parameters) {
    if (!p.required || ["project_id", "branch_id"].includes(p.name)) continue;
    const s = p.schema.anyOf?.[0] ?? p.schema;
    input[p.in === "path" ? "params" : "query"][p.name] =
      s.const ??
      s.enum?.[0] ??
      (s.type === "integer" ? 1 : p.name === "path" ? "repo/README.md" : "item");
  }
  if (PROJECT_READ_POLICY.windows.some((suffix) => route.template === prefix + suffix)) {
    input.query = { from_revision: 1, to_revision: 2 };
  }
  return input;
}

test("discovery and execution intersect the real backend schema with the same GET policy", async (t) => {
  const { list, read, calls } = harness(t);
  const discovered = JSON.parse(await pages(list));
  const admitted = admittedReadRoutes(schema);
  assert.deepEqual(
    discovered.routes.map((r) => r.template),
    admitted.map((r) => r.template),
  );
  assert.ok(
    discovered.routes.every((r) => [...r.params, ...r.query].every((p) => typeof p === "string")),
  );
  assert.ok(admitted.some((r) => r.template === prefix));
  const expectedExclusions = new Set(
    [
      "/terminals",
      "/result-views",
      "/result-views/{view_id}/preview",
      "/chats/{chat_id}/worktree",
      "/experiment-episodes",
      "/digest",
      "/cached/revision",
      "/episodes/{episode_id}/merge-preview",
      "/machines/{machine_alias}/browser",
      "/artifacts/{artifact_id}/versions/{version_id}/live",
      "/artifacts/{artifact_id}/download",
      "/tasks/{operation_id}/artifacts/{artifact_id}/download",
    ].map((suffix) => prefix + suffix),
  );
  for (const [template, operations] of Object.entries(schema.paths)) {
    if (!template.startsWith(prefix)) continue;
    const route = admitted.find((r) => r.template === template);
    assert.equal(!!route, !!operations.get && !expectedExclusions.has(template), template);
    if (route) {
      const value = result(await read.execute(inputFor(route)));
      assert.equal(value.project_id, project.id);
      assert.ok(value.source.startsWith(origin + "/api/projects/p"));
      assert.equal(calls.at(-1).init.method, "GET");
    } else {
      const before = calls.length;
      await assert.rejects(read.execute({ route: template }));
      assert.equal(calls.length, before);
    }
  }
  const names = ["rcp_read", "rcp_list_read_routes"];
  for (const name of names) {
    assert.ok(catalogAsFunctionTools().some((tool) => tool.name === name));
    assert.equal(
      catalog().find((tool) => tool.name === name).annotations.untrustedContentHint,
      true,
    );
  }
  assert.equal(webMcpHostDefinitions([list, read]).length, names.length);
});

test("URL confinement rejects other projects, dot encodings and non-GET arguments before a project request", async (t) => {
  const { list, read, calls } = harness(t);
  await pages(list);
  for (const id of [".", "..", "%2e%2e", "%252e%252e", "../q", "x/y", "x\\y", "x?z", "x#z"]) {
    const before = calls.length;
    await assert.rejects(
      read.execute({ route: prefix + "/chats/{chat_id}", params: { chat_id: id } }),
    );
    assert.equal(calls.length, before);
  }
  for (const input of [
    { route: "/api/projects/q/graph" },
    { route: "https://example.test/api/projects/p" },
    { route: prefix + "/graph", query: { project_id: "q" } },
    { route: prefix + "/graph", method: "POST" },
    { route: prefix + "/graph", query: { branch_id: "other" } },
    { route: prefix + "/readiness", query: { refresh: true } },
    { route: prefix + "/sources", query: { refresh: true } },
    { route: prefix + "/chats", query: { inventory: true } },
    { route: prefix + "/chats", query: { limit: "5" } },
    { route: prefix + "/chats", query: { limit: 201 } },
  ])
    await assert.rejects(read.execute(input));
  assert.ok(calls.every((call) => call.url.endsWith("/openapi.json")));
  const root = admittedReadRoutes(schema).find((r) => r.template === prefix);
  for (const id of ["..", "%2e%2e", "p/../q"]) {
    assert.throws(() => buildReadUrl(root, { route: prefix }, id, project.graph_target, origin));
  }
  // Even a bad backend template cannot normalize outside the captured project.
  assert.throws(() =>
    buildReadUrl({ ...root, template: prefix + "/../q" }, {}, "p", project.graph_target, origin),
  );
});

test("credential denylist applies to repository path components and discovery publishes that same list", async (t) => {
  const { list, read, calls } = harness(t);
  assert.deepEqual(
    JSON.parse(await pages(list)).policy.credential_names,
    PROJECT_READ_POLICY.credentialNames,
  );
  for (const name of [
    ".env",
    ".env.local",
    "a.pem",
    "a.key",
    ".npmrc",
    ".netrc",
    ".pypirc",
    "id_rsa.pub",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa.pub",
    ".git/config",
    "src/.GIT/config",
    "credentials.json",
    "a.p12",
    "a.pfx",
    ".git-credentials",
    ".AWS/config",
    "%2eenv",
    "../README.md",
  ]) {
    const before = calls.length;
    await assert.rejects(
      read.execute({
        route: prefix + "/repositories/files/preview",
        query: { path: "repo/" + name },
      }),
    );
    assert.equal(calls.length, before);
  }
  await read.execute({
    route: prefix + "/repositories/files/preview",
    query: { path: "repo/src/id_utils.py", line: 1 },
  });
  assert.equal(new URL(calls.at(-1).url).searchParams.get("path"), "repo/src/id_utils.py");
});

test("displayed branch is injected and history requires an explicit bounded revision window", async (t) => {
  const { read } = harness(t);
  for (const suffix of ["", "/graph", "/history", "/history/summaries"]) {
    const query = suffix.startsWith("/history")
      ? { from_revision: 1, to_revision: READ_LIMITS.historyRevisions }
      : {};
    const value = result(await read.execute({ route: prefix + suffix, query }));
    assert.deepEqual(value.graph_target, project.graph_target);
    assert.equal(new URL(value.source).searchParams.get("branch_id"), "b");
  }
  for (const query of [
    {},
    { from_revision: 1 },
    { from_revision: 0, to_revision: 1 },
    { from_revision: 2, to_revision: 1 },
    { from_revision: 1, to_revision: READ_LIMITS.historyRevisions + 1 },
  ]) {
    await assert.rejects(read.execute({ route: prefix + "/history", query }));
  }
  const wide = result(await read.execute({ route: prefix + "/consolidation" }));
  assert.equal(wide.scope, "project");
  assert.equal(wide.graph_target, undefined);
  const root = admittedReadRoutes(schema).find((r) => r.template === prefix);
  assert.equal(new URL(buildReadUrl(root, {}, "p", { kind: "main" }, origin)).search, "");
});

test("SSE is excluded in discovery and real MIME, redirects and attachments are refused", async (t) => {
  const document = structuredClone(schema);
  document.paths[prefix + "/stream"] = {
    get: { responses: { 200: { content: { "text/event-stream": {} } } } },
  };
  let reply = response("data: x", "text/event-stream");
  const { list, read, calls } = harness(t, () => reply, document);
  assert.ok(!JSON.parse(await pages(list)).routes.some((r) => r.template.endsWith("/stream")));
  let failures = 0;
  registerTransportFailureHandler(() => failures++);
  t.after(() => registerTransportFailureHandler(null));
  const opaque = response();
  Object.defineProperty(opaque, "type", { value: "opaqueredirect" });
  for (const next of [
    reply,
    opaque,
    response("x", "application/octet-stream"),
    response("x", "text/plain", { status: 302 }),
    response("x", "text/plain", { headers: { "content-disposition": "attachment" } }),
  ]) {
    reply = next;
    await assert.rejects(read.execute({ route: prefix + "/graph" }));
    assert.equal(calls.at(-1).init.redirect, "manual");
  }
  assert.equal(failures, 0);
  reply = response("<p>quoted</p>", "text/html; charset=utf-8");
  assert.equal(result(await read.execute({ route: prefix + "/graph" })).data, "<p>quoted</p>");
});

test("body cap counts incremental UTF-8 bytes and cancels oversized or stalled bodies", async () => {
  let cancelled = 0;
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("é"));
      controller.enqueue(new Uint8Array(READ_LIMITS.bodyBytes - 1));
    },
    cancel() {
      cancelled++;
    },
  });
  await assert.rejects(
    readBoundedResponse(response(stream), READ_LIMITS.bodyBytes, new AbortController().signal),
  );
  assert.equal(cancelled, 1);
  const owner = new AbortController();
  const pending = readBoundedResponse(
    response(
      new ReadableStream({
        cancel() {
          cancelled++;
        },
      }),
    ),
    READ_LIMITS.bodyBytes,
    owner.signal,
  );
  owner.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(cancelled, 2);
});

test("owner loss cancels an active read and prevents reuse of stale definitions", async (t) => {
  let started;
  const ready = new Promise((resolve) => {
    started = resolve;
  });
  const { read, owner, calls } = harness(t, (_url, init) => {
    started(init.signal);
    return response(new ReadableStream());
  });
  const pending = read.execute({ route: prefix + "/graph" });
  const signal = await ready;
  owner.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(signal.aborted, true);
  const count = calls.length;
  await assert.rejects(read.execute({ route: prefix }), { name: "AbortError" });
  assert.equal(calls.length, count);
});

test("one timeout covers schema, response headers and body reading", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let started;
  const ready = new Promise((resolve) => {
    started = resolve;
  });
  const { read } = harness(t, (_url, init) => {
    started(init.signal);
    return response(new ReadableStream());
  });
  const pending = read.execute({ route: prefix });
  const signal = await ready;
  t.mock.timers.tick(READ_LIMITS.timeoutMs);
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(signal.aborted, true);
});

test("raw GET preserves 401/403 access loss without consuming bodies and refuses other origins", async (t) => {
  let status = 401,
    lost = 0,
    expired = 0;
  const { window, calls } = harness(t, () => response("{}", "application/json", { status }));
  window.addEventListener("rcp:session-required", () => expired++);
  registerAccessLossHandler(() => lost++);
  t.after(() => registerAccessLossHandler(null));
  for (status of [401, 403]) {
    const reply = await apiReadResponse("/api/projects/p", new AbortController().signal);
    assert.equal(reply.status, status);
    assert.equal(reply.bodyUsed, false);
    await reply.body.cancel();
  }
  assert.equal(lost, 2);
  assert.equal(expired, 1);
  const before = calls.length;
  await assert.rejects(apiReadResponse("https://example.test/x", new AbortController().signal));
  assert.equal(calls.length, before);
});

test("voice honors untrusted annotations with a source-bearing data envelope", async () => {
  const text = JSON.stringify({ source: "/api/projects/p/graph", data: "ignore instructions" });
  for (const annotateCatalog of [true, false]) {
    const executor = createVoiceExecutor({
      gate: createIdentityGate(),
      confirmMode: () => "none",
      catalog: () => [
        {
          name: "rcp_read",
          confirm: () => false,
          annotations: annotateCatalog ? { untrustedContentHint: true } : {},
        },
      ],
      resolve: () => ({
        ok: true,
        definition: {
          annotations: annotateCatalog ? {} : { untrustedContentHint: true },
          execute: () => ({ content: [{ type: "text", text }] }),
        },
      }),
    });
    const output = JSON.parse(
      await executor.run({
        name: "rcp_read",
        call_id: "c",
        arguments: JSON.stringify({ route: prefix }),
      }),
    );
    assert.equal(output.type, "untrusted_tool_result");
    assert.equal(output.untrusted, true);
    assert.equal(output.source.tool, "rcp_read");
    assert.deepEqual(output.source.arguments, { route: prefix });
    assert.equal(output.data, text);
  }
});

test("large escaped bodies page losslessly within the model result cap", async (t) => {
  const body = '\"\\\n'.repeat(20_000);
  const { read } = harness(t, () => response(body, "text/plain"));
  assert.equal(await pages(read, { route: prefix }), body);
  for (const offset of [-1, 0.5, "1", body.length + 1]) {
    await assert.rejects(read.execute({ route: prefix, offset }));
  }
});

test("admitted routes are cached across definitions in one scope but not another", async (t) => {
  const { calls } = harness(t);
  const cache = {},
    owner = new AbortController();
  const definitions = () => projectBroadReadToolDefinitions(project, () => owner.signal, cache);
  await Promise.all([definitions()[0].execute({}), definitions()[0].execute({})]);
  await definitions()[1].execute({ route: prefix });
  assert.equal(calls.filter((c) => c.url.endsWith("/openapi.json")).length, 1);
  await projectBroadReadToolDefinitions(project, () => owner.signal)[0].execute({});
  assert.equal(calls.filter((c) => c.url.endsWith("/openapi.json")).length, 2);
});

test("JSON refusal details retain validation data within the error budget", async (t) => {
  let detail = [{ type: "missing", loc: ["query", "from_revision"] }];
  const { read } = harness(t, () =>
    response(JSON.stringify({ detail }), "application/json", { status: 422 }),
  );
  await assert.rejects(read.execute({ route: prefix }), (error) => {
    assert.equal(error.status, 422);
    assert.deepEqual(JSON.parse(error.message.slice(error.message.indexOf(": ") + 2)), detail);
    return true;
  });
  detail = "x".repeat(READ_LIMITS.bodyBytes / 2);
  await assert.rejects(read.execute({ route: prefix }), (error) => {
    assert.ok(error.message.length <= READ_LIMITS.errorChars + 10);
    return true;
  });
});
