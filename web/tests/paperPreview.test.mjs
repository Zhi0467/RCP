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
