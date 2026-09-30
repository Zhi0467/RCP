import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import vm from "node:vm";
import test from "node:test";
import { chromium } from "playwright";

const script = readFileSync(new URL("../../src/rcp/artifact_live.js", import.meta.url), "utf8");

const settle = () => new Promise((resolve) => setImmediate(resolve));
function liveShell() {
  const listeners = {},
    messages = [];
  const frame = { contentWindow: { postMessage: (value) => messages.push(value) } };
  let fetches = 0,
    scheduled;
  const payload = {
    kind: "rcp-live-data",
    version: 1,
    complete: true,
    final: false,
    refresh_seconds: 2,
    snapshots: [],
  };
  const document = {
    hidden: false,
    getElementById: (id) => (id === "preview" ? frame : { textContent: "" }),
    addEventListener: (name, fn) => {
      listeners[name] = fn;
    },
  };
  vm.runInNewContext(script, {
    document,
    defaultLiveDelay: 2000,
    liveUrl: "/pinned-version/live",
    window: {
      addEventListener: (name, fn) => {
        listeners[name] = fn;
      },
    },
    setTimeout: (fn) => {
      scheduled = fn;
      return 1;
    },
    clearTimeout: () => {
      scheduled = null;
    },
    fetch: async (url) => {
      assert.equal(url, "/pinned-version/live");
      fetches++;
      return { ok: true, json: async () => ({ ...payload }) };
    },
  });
  return {
    document,
    payload,
    listeners,
    messages,
    get fetches() {
      return fetches;
    },
    get scheduled() {
      return scheduled;
    },
    ready: (source = frame.contentWindow) =>
      listeners.message({
        source,
        data: { type: "rcp-live-ready", version: 1 },
      }),
  };
}

test("shell fetches only while visible, rejects foreign readiness, and stops after final data", async () => {
  const app = liveShell();
  app.ready({});
  await settle();
  assert.equal(app.fetches, 0);
  app.ready();
  await settle();
  assert.equal(app.fetches, 1);
  assert.equal(app.messages.length, 1);
  assert.equal(typeof app.scheduled, "function");
  app.document.hidden = true;
  app.listeners.visibilitychange();
  await settle();
  assert.equal(app.scheduled, null);
  assert.equal(app.fetches, 1);
  app.payload.final = true;
  app.payload.complete = false;
  app.document.hidden = false;
  app.listeners.visibilitychange();
  await settle();
  assert.equal(app.fetches, 2);
  assert.equal(typeof app.scheduled, "function");
  app.payload.complete = true;
  await app.scheduled();
  assert.equal(app.fetches, 3);
  assert.equal(app.messages.at(-1).final, true);
  assert.equal(app.scheduled, null);
  app.listeners.visibilitychange();
  await settle();
  assert.equal(app.fetches, 3);
});

test("opaque HTML receives live data through the private channel without network authority", async () => {
  const rendered = JSON.parse(
    execFileSync(
      "uv",
      [
        "run",
        "python",
        "-c",
        `
import json
from rcp.artifact_views import artifact_content, artifact_viewer_document
from rcp.artifacts import AgentArtifactDescriptor
source=b'''<p id="value">Waiting</p><script>window.addEventListener('message',e=>{if(e.data?.kind==='rcp-live-data')document.getElementById('value').textContent=String(e.data.snapshots[0].rows[0]);});</script>'''
content,_,csp=artifact_content('live.html','text/html',source)
shell,scsp=artifact_viewer_document(AgentArtifactDescriptor(artifact_id='a'*24,name='live.html',media_type='text/html'),content_url='/content',state='temporary',live_url='/live')
print(json.dumps(dict(content=content,csp=csp,shell=shell,scsp=scsp)))
`,
      ],
      { cwd: new URL("../..", import.meta.url), encoding: "utf8" },
    ),
  );
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("http://live.test/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/live")
        return route.fulfill({
          json: {
            kind: "rcp-live-data",
            version: 1,
            complete: true,
            final: true,
            refresh_seconds: 2,
            snapshots: [{ kind: "file", rows: [42], truncated: false }],
          },
        });
      const content = path === "/content";
      return route.fulfill({
        contentType: "text/html",
        body: rendered[content ? "content" : "shell"],
        headers: { "Content-Security-Policy": rendered[content ? "csp" : "scsp"] },
      });
    });
    await page.goto("http://live.test/viewer");
    const artifact = page.frameLocator("#preview").frameLocator("#artifact");
    await artifact.locator("#value").filter({ hasText: "42" }).waitFor();
    const frame = page.frames().find((frame) => frame.url() === "about:srcdoc");
    assert.equal(
      await frame.evaluate(() => {
        try {
          return parent.document.title;
        } catch {
          return "opaque";
        }
      }),
      "opaque",
    );
    assert.equal(
      await frame.evaluate(async () => {
        try {
          await fetch("http://live.test/live");
          return "allowed";
        } catch {
          return "blocked";
        }
      }),
      "blocked",
    );
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});

test("invalid declarations stay static without forwarding data or polling", async () => {
  const app = liveShell();
  Object.assign(app.payload, { static: true, reason: "Unknown launch key" });
  app.ready();
  await settle();
  assert.equal(app.scheduled, null);
  assert.deepEqual(app.messages, []);
  app.listeners.visibilitychange();
  await settle();
  assert.equal(app.fetches, 1);
});
