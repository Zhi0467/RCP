import assert from "node:assert/strict";
import test from "node:test";
import { markdownPaperPath, paperRelativePath, typstRoots } from "../src/paper/typstPreview.ts";

test("paper references remain inside the tree", () => {
  assert.equal(
    paperRelativePath("sections/intro.md", "../figures/result.png"),
    "figures/result.png",
  );
  assert.equal(paperRelativePath("intro.md", "../secret.png"), null);
  assert.equal(paperRelativePath("intro.md", "https://example.com/a.png"), null);
});

test("Markdown URLs decode and drop fragments before resolving inside the tree", () => {
  assert.equal(markdownPaperPath("intro.md", "figures/my%20plot.png"), "figures/my plot.png");
  assert.equal(markdownPaperPath("a/intro.md", "section.md#results"), "a/section.md");
  assert.equal(markdownPaperPath("intro.md", "%2e%2e/secret.png"), null);
  assert.equal(markdownPaperPath("intro.md", "bad%zz.png"), null);
});

test("Typst root candidates only include ancestors of the open file", () => {
  const sources = new Map([
    ["paper.typ", '#include "sections/intro.typ"'],
    ["other.typ", "= Other paper"],
    ["sections/intro.typ", "= Introduction"],
    ["variant.typ", '#import "sections/intro.typ": *'],
  ]);
  assert.deepEqual(typstRoots("sections/intro.typ", sources), ["paper.typ", "variant.typ"]);
  assert.deepEqual(typstRoots("other.typ", sources), ["other.typ"]);
});

test("Markdown preview resolves paper references without navigating outside the tree", async () => {
  const { createServer } = await import("vite");
  const { createElement } = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const server = await createServer({
    root: new URL("..", import.meta.url).pathname,
    configFile: false,
    optimizeDeps: { noDiscovery: true },
    server: { middlewareMode: true, hmr: false },
    appType: "custom",
  });
  try {
    const { PaperPreview } = await server.ssrLoadModule("/src/paper/PaperPreview.tsx");
    const file = {
      file_id: "intro",
      path: "sections/intro.md",
      kind: "text",
      format: "markdown",
      size: 20,
    };
    const image = {
      file_id: "figure",
      path: "figures/result.png",
      kind: "binary",
      format: "image",
      size: 50,
      sha256: "abc",
    };
    const html = renderToStaticMarkup(
      createElement(PaperPreview, {
        projectId: "p",
        file,
        tree: {
          revision: 3,
          saved_revision: 3,
          published_revision: 2,
          publish_state: "pending",
          hold: null,
          files: [file, image],
        },
        text: "![plot](../figures/result.png) [local](intro.md) [external](https://example.org/) [missing](missing.md) [unsafe](javascript:alert)",
        onOpenFile() {},
        active: true,
      }),
    );
    assert.match(html, /<img[^>]*src="\/api\/projects\/p\/paper\/files\/figure\/raw\?sha256=abc"/);
    assert.doesNotMatch(html, /src="\.\.\/figures/);
    assert.match(html, /<a href="#">local<\/a>/);
    assert.match(
      html,
      /<a href="https:\/\/example.org\/" target="_blank" rel="noopener noreferrer">external<\/a>/,
    );
    assert.match(html, /<span>missing<\/span>/);
    assert.match(html, /<span>unsafe<\/span>/);
  } finally {
    await server.close();
  }
});

test("Typst dependencies respect strings, escapes, raw text and nested comments", () => {
  const sources = new Map([
    [
      "main.typ",
      '#let url = "https://example.org"; #include /* outer /* nested */ ok */ "part.typ"',
    ],
    [
      "fake.typ",
      '#let text = "include \\"part.typ\\""; // #include "part.typ"\n/* #include "part.typ" */\n`#include "part.typ"`',
    ],
    ["part.typ", "= Part"],
  ]);
  assert.deepEqual(typstRoots("part.typ", sources), ["main.typ"]);
});

test("Typst tree fetches only changed hashes and overlays unsaved open text", async () => {
  const { readTypstTree } = await import("../src/paper/typstPreview.ts");
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url) => {
    requests.push(url);
    return new Response("saved");
  };
  try {
    const cache = new Map();
    const file = { file_id: "main", path: "main.typ", sha256: "main" };
    const part = { file_id: "part", path: "part.typ", sha256: "one" };
    const signal = new AbortController().signal;
    await readTypstTree("p", [file, part], 1, file, "first", signal, cache);
    const next = await readTypstTree("p", [file, part], 2, file, "second", signal, cache);
    assert.equal(requests.length, 1);
    assert.equal(new TextDecoder().decode(next[0].bytes), "second");
    await readTypstTree("p", [file, { ...part, sha256: "two" }], 3, file, "third", signal, cache);
    assert.equal(requests.length, 2);
    assert.match(requests[1], /revision=3$/);
    assert.equal(cache.size, 1);
  } finally {
    globalThis.fetch = original;
  }
});

test("Typst compiler reuses its worker, ignores superseded replies and survives source errors", async () => {
  const { TypstCompiler } = await import("../src/paper/typstPreview.ts");
  const original = globalThis.Worker;
  const workers = [];
  globalThis.Worker = class {
    terminated = false;
    constructor() {
      workers.push(this);
    }
    postMessage(data) {
      this.request = data;
    }
    terminate() {
      this.terminated = true;
    }
    reply(data, id = this.request.id) {
      this.onmessage({ data: { id, ...data } });
    }
  };
  const compiler = new TypstCompiler();
  try {
    const controller = new AbortController();
    const first = compiler.compile([], "main.typ", controller.signal);
    workers[0].reply({ pdf: new Uint8Array([1]) });
    assert.deepEqual(await first, new Uint8Array([1]));
    const stale = compiler.compile([], "main.typ", controller.signal);
    const rejected = assert.rejects(stale, { name: "AbortError" });
    const oldId = workers[0].request.id;
    controller.abort();
    await rejected;
    const next = compiler.compile([], "main.typ", new AbortController().signal);
    const failed = assert.rejects(next, /bad source/);
    workers[0].reply({ pdf: new Uint8Array([2]) }, oldId);
    workers[0].reply({ error: "bad source" });
    await failed;
    // A source error keeps the warm worker for the next edit.
    assert.equal(workers[0].terminated, false);
    const recovery = compiler.compile([], "main.typ", new AbortController().signal);
    workers[0].reply({ pdf: new Uint8Array([3]) });
    await recovery;
    assert.equal(workers.length, 1);
    compiler.dispose();
    assert.equal(workers[0].terminated, true);
  } finally {
    compiler.dispose();
    globalThis.Worker = original;
  }
});

test("Typst worker requests resolved diagnostics and formats source locations", async () => {
  const { mkdtemp, mkdir, writeFile, readFile, rm } = await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { pathToFileURL } = await import("node:url");
  const { stripTypeScriptTypes } = await import("node:module");
  const directory = await mkdtemp(`${tmpdir()}/paper-worker-`);
  const original = globalThis.self;
  const originalWindow = globalThis.window;
  const replies = [];
  const options = [];
  const diagnostics = [
    { path: "/sections/part.typ", range: "3:2-3:5", message: "bad input" },
    { path: "", range: "", message: "no location" },
  ];
  globalThis.self = {
    location: { origin: pathToFileURL(directory).href },
    postMessage: (reply) => replies.push(reply),
    runtime: {
      setCompilerInitOptions() {},
      use() {},
      async resetShadow() {},
      async mapShadow() {},
      async getCompiler() {
        return {
          async reset() {},
          async compile(value) {
            options.push(value);
            return { diagnostics };
          },
        };
      },
    },
  };
  try {
    await mkdir(`${directory}/api/paper-assets/typst`, { recursive: true });
    await writeFile(
      `${directory}/api/paper-assets/typst/all-in-one-lite.bundle.js`,
      "export const $typst = globalThis.self.runtime; export const TypstSnippet = {preloadFontAssets() {}};",
    );
    const source = await readFile(new URL("../src/paper/typst.worker.ts", import.meta.url), "utf8");
    await import(
      `data:text/javascript;base64,${Buffer.from(stripTypeScriptTypes(source)).toString("base64")}`
    );
    await globalThis.self.onmessage({ data: { id: 1, root: "main.typ", sources: [] } });
    assert.deepEqual(options, [{ mainFilePath: "/main.typ", format: 1, diagnostics: "full" }]);
    assert.deepEqual(replies, [{ id: 1, error: "/sections/part.typ:4: bad input\nno location" }]);
    // A tree queued behind a failing compile is still compiled and answered under its own id.
    const running = globalThis.self.onmessage({ data: { id: 2, root: "main.typ", sources: [] } });
    await globalThis.self.onmessage({ data: { id: 3, root: "main.typ", sources: [] } });
    await running;
    assert.equal(options.length, 3);
    assert.deepEqual(
      replies.map((reply) => reply.id),
      [1, 2, 3],
    );
  } finally {
    globalThis.self = original;
    globalThis.window = originalWindow;
    await rm(directory, { recursive: true, force: true });
  }
});

test("large Markdown preview waits until typing pauses", async (t) => {
  const { createPaperViteServer } = await import("./paperTestServer.mjs");
  const { chromium } = await import("playwright");
  const { server, close } = await createPaperViteServer([
    {
      name: "preview-idle-test",
      resolveId(id) {
        if (id === "/preview-idle.tsx") return id;
      },
      load(id) {
        if (id !== "/preview-idle.tsx") return;
        return `import React from 'react';
          import {createRoot} from 'react-dom/client';
          import {usePreviewText} from '/src/paper/usePreviewText.ts';
          const root = createRoot(document.getElementById('root'));
          function Preview({text}) { return <output>{usePreviewText(text, 'file', true).slice(-1)}</output>; }
          window.update = suffix => root.render(<Preview text={'x'.repeat(1500000) + suffix}/>);
          window.update('a');`;
      },
    },
  ]);
  t.after(close);
  const browser = await chromium.launch({ headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.route("**/preview-idle.html", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await server.transformIndexHtml(
        "/preview-idle.html",
        '<div id="root"></div><script type="module" src="/preview-idle.tsx"></script>',
      ),
    }),
  );
  await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/preview-idle.html`);
  await page.waitForFunction(() => document.querySelector("output")?.textContent === "a");
  await page.clock.install();
  for (let i = 0; i < 10; i++) {
    await page.evaluate((i) => window.update(String(i)), i);
    await page.clock.fastForward(200);
    assert.equal(await page.locator("output").textContent(), "a");
  }
  await page.clock.fastForward(1000);
  await page.waitForFunction(() => document.querySelector("output")?.textContent === "9");
});
