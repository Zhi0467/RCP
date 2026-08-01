import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiError,
  api,
  clearProjectCaches,
  loadProjectReadiness,
  pinApiInstance,
  registerMutationFailureHandler,
} from "../src/api.ts";

const metrics = {
  remote_sources: {
    bytes: 128,
    count: 2,
    limits: { max_bytes: 1024, max_count: 8, ttl_seconds: 86400 },
    oldest_accessed_at: "2026-07-28T00:00:00Z",
    reclaimable_bytes: 64,
    reclaimable_count: 1,
  },
  session_slices: {
    bytes: 0,
    count: 0,
    limits: { max_bytes: 2048, max_count: 16, ttl_seconds: 172800 },
    oldest_accessed_at: null,
    reclaimable_bytes: 0,
    reclaimable_count: 0,
  },
};

test("clearProjectCaches issues one DELETE and returns replacement metrics", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, init) => {
    request = { path, init };
    return new Response(JSON.stringify(metrics), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    assert.deepEqual(await clearProjectCaches("/api/projects/demo"), metrics);
    assert.equal(request.path, "/api/projects/demo/caches");
    assert.equal(request.init.method, "DELETE");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("automatic readiness reads cached state while explicit refresh bypasses it", async () => {
  const originalFetch = globalThis.fetch;
  const paths = [];
  globalThis.fetch = async (path) => {
    paths.push(path);
    return new Response(JSON.stringify({ provider_readiness: {}, providers: {} }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await loadProjectReadiness("/api/projects/demo");
    await loadProjectReadiness("/api/projects/demo", true);
    assert.deepEqual(paths, [
      "/api/projects/demo/readiness",
      "/api/projects/demo/readiness?refresh=true",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("API errors retain status for stale and active-task handling", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "Agent task is active" }), {
    status: 409,
    headers: { "Content-Type": "application/json" },
  });
  try {
    await assert.rejects(
      clearProjectCaches("/api/projects/demo"),
      (error) => error instanceof ApiError && error.status === 409 && error.message === "Agent task is active",
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a failed mutation runs the registered identity verifier once", async () => {
  const originalFetch = globalThis.fetch;
  let checkedPath = null;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "Conflict" }), {
    status: 409,
    headers: { "Content-Type": "application/json" },
  });
  registerMutationFailureHandler(async (path) => { checkedPath = path; });
  try {
    await assert.rejects(
      api("/api/projects/demo/sync", { method: "POST", body: "{}" }),
      (error) => error instanceof ApiError && error.status === 409,
    );
    assert.equal(checkedPath, "/api/projects/demo/sync");
  } finally {
    registerMutationFailureHandler(null);
    globalThis.fetch = originalFetch;
  }
});

test("mutations carry the pinned backend identity and preserve caller headers", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, init) => {
    request = { path, init };
    return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
  };
  pinApiInstance("instance-a");
  try {
    await api("/api/projects/demo/sync", {
      method: "POST",
      headers: new Headers({ Authorization: "Bearer test", "X-Caller": "kept" }),
      body: "{}",
    });
    const headers = new Headers(request.init.headers);
    assert.equal(headers.get("X-RCP-Instance-ID"), "instance-a");
    assert.equal(headers.get("Authorization"), "Bearer test");
    assert.equal(headers.get("X-Caller"), "kept");
  } finally {
    pinApiInstance(null);
    globalThis.fetch = originalFetch;
  }
});

test("reads never carry the pinned backend identity", async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, init) => {
    request = { path, init };
    return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
  };
  pinApiInstance("instance-a");
  try {
    await api("/api/projects/demo", { headers: { "X-Caller": "kept" } });
    const headers = new Headers(request.init.headers);
    assert.equal(headers.get("X-RCP-Instance-ID"), null);
    assert.equal(headers.get("X-Caller"), "kept");
  } finally {
    pinApiInstance(null);
    globalThis.fetch = originalFetch;
  }
});
