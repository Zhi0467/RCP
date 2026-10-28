import assert from "node:assert/strict";
import test from "node:test";
import { paperRelativePath, typstRoots } from "../src/paper/typstPreview.ts";

test("paper references remain inside the tree", () => {
  assert.equal(
    paperRelativePath("sections/intro.md", "../figures/result.png"),
    "figures/result.png",
  );
  assert.equal(paperRelativePath("intro.md", "../secret.png"), null);
  assert.equal(paperRelativePath("intro.md", "https://example.com/a.png"), null);
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

test("Markdown preview embeds a paper image through its raw file endpoint", async () => {
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
        text: "![plot](../figures/result.png)",
      }),
    );
    assert.match(html, /<img[^>]*src="\/api\/projects\/p\/paper\/files\/figure\/raw\?revision=3"/);
    assert.doesNotMatch(html, /src="\.\.\/figures/);
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

test("Typst compiler reuses its worker, ignores superseded replies and recovers after error", async () => {
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
    assert.equal(workers.length, 1);
    assert.equal(workers[0].terminated, true);
    const recovery = compiler.compile([], "main.typ", new AbortController().signal);
    workers[1].reply({ pdf: new Uint8Array([3]) });
    await recovery;
    compiler.dispose();
    assert.equal(workers[1].terminated, true);
  } finally {
    compiler.dispose();
    globalThis.Worker = original;
  }
});
