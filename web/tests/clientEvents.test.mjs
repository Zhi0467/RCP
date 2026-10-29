import assert from "node:assert/strict";
import { test } from "node:test";

import { installClientEventReporting, reportClientEvent } from "../src/core/clientEvents.ts";

function recordingSend(result = () => Promise.resolve({ ok: true })) {
  const calls = [];
  const send = (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    return result();
  };
  return { calls, send };
}

function errorEvent(error) {
  return Object.assign(new Event("error"), { error, message: `Uncaught ${error}` });
}

test("a window error and an unhandled rejection each send exactly one event", () => {
  globalThis.location = { hash: "#/projects/p1?view=dag&owner-code=secret" };
  try {
    const target = new EventTarget();
    const { calls, send } = recordingSend();
    installClientEventReporting(target, send);

    target.dispatchEvent(errorEvent(new TypeError("boom")));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/client-events");
    assert.equal(calls[0].init.method, "POST");
    assert.deepEqual(calls[0].body, {
      kind: "window_error",
      message: "TypeError: boom",
      view: "dag",
    });
    // Only the view name leaves the page, never the hash that may hold a code.
    assert.equal(calls[0].init.body.includes("secret"), false);

    target.dispatchEvent(Object.assign(new Event("unhandledrejection"), { reason: "nope" }));
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1].body, { kind: "unhandled_rejection", message: "nope", view: "dag" });
  } finally {
    delete globalThis.location;
  }
});

test("a failed send is swallowed: no throw, no retry, no second event", async () => {
  const unhandled = [];
  const onUnhandled = (reason) => unhandled.push(reason);
  process.on("unhandledRejection", onUnhandled);
  try {
    const target = new EventTarget();
    const rejecting = recordingSend(() => Promise.reject(new TypeError("Failed to fetch")));
    installClientEventReporting(target, rejecting.send);
    target.dispatchEvent(errorEvent(new Error("first")));

    const throwing = recordingSend(() => {
      throw new Error("send exploded");
    });
    assert.doesNotThrow(() => reportClientEvent("notice", "toast", throwing.send));

    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(rejecting.calls.length, 1);
    assert.equal(throwing.calls.length, 1);
    assert.deepEqual(unhandled, []);
  } finally {
    process.off("unhandledRejection", onUnhandled);
  }
});

test("outside a project the view is the index and a long message is cut", () => {
  const { calls, send } = recordingSend();
  reportClientEvent("notice", "x".repeat(5_000), send);
  assert.ok(calls[0].body.message.length < 5_000);
  assert.equal(calls[0].body.view, "index");
});
