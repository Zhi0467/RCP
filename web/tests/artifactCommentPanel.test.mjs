import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

const script = readFileSync(
  new URL("../../src/rcp/artifact_comment_panel.js", import.meta.url),
  "utf8",
);
const settle = () => new Promise((resolve) => setImmediate(resolve));
function shell() {
  const element = () => ({
    value: "",
    hidden: false,
    disabled: false,
    listeners: {},
    addEventListener(name, handler) {
      this.listeners[name] = handler;
    },
    replaceChildren() {},
    append() {},
    focus() {},
    setAttribute() {},
    querySelector() {
      return this.child ?? (this.child = element());
    },
    after(child) {
      this.afterElement = child;
    },
  });
  const elements = Object.fromEntries(
    [
      "items",
      "count",
      "tray",
      "queue",
      "editNow",
      "send",
      "general",
      "notice",
      "message",
      "composer",
    ].map((id) => [id, element()]),
  );
  const listeners = {};
  const timers = new Map();
  const requests = [];
  const messages = [];
  const saved = new Map();
  let canComment = true;
  let status = 200;
  let sendStatus = 409;
  let timerId = 0;
  const context = {
    config: {
      projectId: "p",
      artifactId: "a",
      stateUrl: "/state",
      commentsUrl: "/comments",
      maxSelections: 10,
      stateRefreshMs: 1000,
    },
    document: {
      hidden: false,
      getElementById: (id) => elements[id] ?? null,
      createElement: element,
      addEventListener: (name, handler) => {
        listeners[name] = handler;
      },
    },
    window: {
      addEventListener: (name, handler) => {
        listeners[name] = handler;
      },
      parent: { postMessage: (...args) => messages.push(args) },
    },
    location: { origin: "https://rcp.example" },
    localStorage: {
      getItem: (key) => saved.get(key),
      setItem: (key, value) => saved.set(key, value),
    },
    installSelectionConfirmation: () => () => {},
    setTimeout(handler) {
      timers.set(++timerId, handler);
      return timerId;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (status === 0) throw new Error("Network interrupted");
      const code = url === "/state" ? status : sendStatus;
      return {
        ok: code === 200,
        status: code,
        json: async () =>
          url === "/state"
            ? { can_comment: canComment, fresh_session_required: false }
            : {
                detail: { code: "fresh_session_required", message: "Start a fresh session" },
                operation_id: "edit",
              },
      };
    },
  };
  vm.runInNewContext(script, context);
  return {
    elements,
    messages,
    saved,
    setCanComment: (value) => {
      canComment = value;
    },
    listeners,
    requests,
    timers,
    setStatus: (value) => {
      status = value;
    },
    setSendStatus: (value) => {
      sendStatus = value;
    },
    tick: async () => {
      const [id, fn] = timers.entries().next().value;
      timers.delete(id);
      await fn();
    },
  };
}

test("Edit now and Send post one comment list, and resubmit only with fresh-session consent", async () => {
  const app = shell();
  await settle();
  const { general, message, editNow, send } = app.elements;
  assert.equal(editNow.disabled, true);
  assert.equal(send.disabled, true);
  general.listeners.click();
  message.value = "Update the plot";
  message.listeners.input();
  assert.equal(editNow.disabled, false);
  editNow.listeners.click();
  await settle();
  const posts = () => app.requests.filter((request) => request.url === "/comments");
  assert.deepEqual(JSON.parse(posts()[0].options.body), {
    comments: [{ text: "Update the plot", selection: null }],
    edit_now: true,
    fresh_session: false,
  });
  // The refused comment stays queued for the tray's one-off send.
  assert.equal(app.messages.length, 0);
  assert.equal(send.disabled, false);
  app.setCanComment(false);
  await app.tick();
  assert.equal(send.disabled, true);
  app.setCanComment(true);
  await app.tick(); // A stale availability projection must not undo the explicit offer.
  app.setSendStatus(200);
  send.listeners.click();
  await settle();
  const second = JSON.parse(posts()[1].options.body);
  assert.equal(second.fresh_session, true);
  assert.equal(second.edit_now, false);
  assert.equal(second.comments.length, 1);
  assert.equal(send.disabled, true);
  for (const { options } of posts()) {
    assert.equal(options.method, "POST");
    assert.equal(options.credentials, "same-origin");
  }
  assert.deepEqual(JSON.parse([...app.saved.values()].at(-1)).comments, []);
  assert.deepEqual(JSON.parse(JSON.stringify(app.messages)), [
    [
      {
        type: "rcp-artifact-edit-started",
        version: 1,
        artifact_id: "a",
        operation_id: "edit",
      },
      "https://rcp.example",
    ],
  ]);
});

test("shell stops permanent state failures until Retry and keeps polling transient failures", async () => {
  for (const status of [403, 404, 410, 0, 409, 503]) {
    const app = shell();
    await settle();
    app.setStatus(status);
    await app.tick();
    const permanent = [403, 404, 410].includes(status);
    assert.equal(app.timers.size, permanent ? 0 : 1);
    assert.equal(app.elements.notice.afterElement.hidden, !permanent);
    if (permanent) {
      const count = app.requests.length;
      await app.listeners.focus();
      assert.equal(app.requests.length, count);
      app.setStatus(200);
      app.elements.notice.afterElement.listeners.click();
      await settle();
      assert.equal(app.requests.length, count + 1);
      assert.equal(app.timers.size, 1);
    }
  }
});
