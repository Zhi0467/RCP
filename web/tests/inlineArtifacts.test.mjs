import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import { stagedArtifactContext } from "../src/chat/chatInput.ts";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { MarkdownAnswer } = await server.ssrLoadModule("/src/core/chatMarkdown.ts");
const {
  INLINE_ARTIFACT_MAX_HEIGHT,
  inlineArtifactFor,
  inlineArtifactNames,
  inlineViewerUrl,
  isInlineViewable,
  readInlineShellMessage,
} = await server.ssrLoadModule("/src/artifacts/inlineArtifacts.ts");
const { announceArtifactVersionChange, onArtifactVersionChange } = await server.ssrLoadModule(
  "/src/artifacts/artifactViewerModel.ts",
);
after(() => server.close());

const dir = "/stage/chats/c/turns/op-1/artifacts";
const artifact = (name, view, extra = {}) => ({
  artifact_id: "0123456789abcdef01234567",
  name,
  media_type: "text/html",
  view,
  available: true,
  unavailable_reason: null,
  can_open: true,
  can_download: true,
  can_keep: true,
  can_discuss: true,
  ...extra,
});

test("a reply embeds the artifacts its own turn wrote, by image syntax only", () => {
  const text = [
    `Play it here: ![Breakout](${dir}/breakout.html)`,
    `![Chart](<${dir}/loss.svg> "Loss")`,
    `A link is not an embed: [log](${dir}/log.txt)`,
    "![other turn](/stage/chats/c/turns/op-2/artifacts/stale.html)",
    `![with line](${dir}/notes.md:4)`,
    "![remote](https://example.com/x.png)",
  ].join("\n\n");
  assert.deepEqual([...inlineArtifactNames(text, "op-1")].sort(), ["breakout.html", "loss.svg"]);
});

test("syntax that renders no image embeds nothing, so the card stays", () => {
  const quoted = [
    "Embed with `![Game](" + dir + "/inline.html)`:",
    "```md\n![Game](" + dir + "/fenced.html)\n```",
    "<!-- ![Game](" + dir + "/commented.html) -->",
    "\\![Game](" + dir + "/escaped.html)",
    "![By reference][game]",
    "",
    "[game]: " + dir + "/referenced.html",
  ].join("\n\n");
  assert.deepEqual([...inlineArtifactNames(quoted, "op-1")], ["referenced.html"]);
});

test("a repeated reference label embeds its first definition, as it renders", () => {
  const text = ["![Plot][p]", "[p]: " + dir + "/first.svg", "[p]: " + dir + "/second.svg"].join(
    "\n\n",
  );
  assert.deepEqual([...inlineArtifactNames(text, "op-1")], ["first.svg"]);
});

test("only artifacts the viewer shows render in place", () => {
  assert.equal(isInlineViewable(artifact("a.html", "html")), true);
  assert.equal(isInlineViewable(artifact("a.svg", "image")), true);
  assert.equal(isInlineViewable(artifact("a.md", "markdown")), true);
  assert.equal(isInlineViewable(artifact("a.csv", "text")), true);
  assert.equal(isInlineViewable(artifact("a.pdf", "pdf")), false);
  assert.equal(isInlineViewable(artifact("a.bin", "file")), false);
  const artifacts = [artifact("breakout.html", "html")];
  assert.equal(inlineArtifactFor(`${dir}/breakout.html`, "op-1", artifacts).artifact, artifacts[0]);
  assert.deepEqual(inlineArtifactFor(`${dir}/missing.html`, "op-1", artifacts), {
    name: "missing.html",
    artifact: null,
  });
  assert.equal(inlineArtifactFor("https://example.com/a.png", "op-1", artifacts), null);
});

test("the inline shell is the viewer URL in its inline presentation", () => {
  assert.equal(
    inlineViewerUrl("/api/projects/p/artifacts/a/viewer"),
    "/api/projects/p/artifacts/a/viewer?presentation=inline",
  );
  assert.equal(
    inlineViewerUrl("/api/projects/p/artifacts/a/viewer?presentation=panel"),
    "/api/projects/p/artifacts/a/viewer?presentation=inline",
  );
});

test("shell messages are read only from that frame and bounded", () => {
  const frame = {};
  const origin = "http://rcp.test";
  const read = (data, source = frame, from = origin) =>
    readInlineShellMessage({ source, origin: from, data }, frame, origin);
  assert.deepEqual(read({ type: "rcp-artifact-size", version: 1, height: 640.2 }), {
    kind: "size",
    height: 641,
  });
  assert.equal(
    read({ type: "rcp-artifact-size", version: 1, height: 1e9 }).height,
    INLINE_ARTIFACT_MAX_HEIGHT,
  );
  assert.equal(read({ type: "rcp-artifact-size", version: 1, height: 40 }, {}), null);
  assert.equal(read({ type: "rcp-artifact-size", version: 1, height: 40 }, frame, "null"), null);
  assert.equal(read({ type: "rcp-artifact-size", version: 1, height: "tall" }), null);
  assert.equal(read({ type: "rcp-artifact-size", version: 2, height: 40 }), null);

  const box = read({
    type: "rcp-artifact-selection",
    version: 1,
    description: "Loss chart",
    selection: {
      kind: "box",
      rect: { x: 0.1, y: 0.2, width: 0.3, height: 0.4 },
      viewport: { width: 800.4, height: 99999 },
      elements: Array.from({ length: 12 }, (_, i) => ({
        path: `g:nth-of-type(${i})`,
        label: "x",
        text: "y",
        extra: 1,
      })),
    },
  });
  assert.equal(box.kind, "selection");
  assert.equal(box.selection.elements.length, 8);
  assert.deepEqual(box.selection.viewport, { width: 800, height: 32768 });
  assert.equal("extra" in box.selection.elements[0], false);
  assert.equal(
    read({
      type: "rcp-artifact-selection",
      version: 1,
      selection: {
        kind: "box",
        rect: { x: -1, y: 0, width: 2, height: 1 },
        viewport: { width: 1, height: 1 },
      },
    }),
    null,
  );
  assert.deepEqual(read({ type: "rcp-artifact-selection", version: 1, selection: null }), {
    kind: "selection",
    selection: null,
    description: "",
  });
});

test("an inline comment travels as the artifact selection of a fresh-session edit", () => {
  const context = stagedArtifactContext([
    {
      id: "a",
      selectedText: "Loss chart",
      comment: " Make it log scale ",
      artifact: {
        context: {
          source: "task",
          operation_id: "op-1",
          artifact_id: "0123456789abcdef01234567",
          fresh_session: true,
        },
        name: "loss.html",
        selection: { kind: "text", text: "Loss", surrounding_text: "Loss by step", comment: "" },
      },
    },
  ]);
  assert.deepEqual(context, {
    source: "task",
    operation_id: "op-1",
    artifact_id: "0123456789abcdef01234567",
    fresh_session: true,
    selections: [
      {
        kind: "text",
        text: "Loss",
        surrounding_text: "Loss by step",
        comment: "Make it log scale",
      },
    ],
  });
});

test("Markdown hands an embedded image to the reply's renderer and keeps other images", () => {
  const rendered = renderToStaticMarkup(
    createElement(MarkdownAnswer, {
      text: `Before\n\n![Breakout](${dir}/breakout.html)\n\n![plain](https://example.com/a.png)`,
      renderEmbed: (src, alt) =>
        src.endsWith("breakout.html") ? createElement("span", { "data-embed": alt }) : null,
    }),
  );
  assert.match(rendered, /<span data-embed="Breakout"><\/span>/);
  assert.match(rendered, /<img src="https:\/\/example.com\/a.png" alt="plain"\/>/);
  // Without a renderer an answer is unchanged.
  assert.match(
    renderToStaticMarkup(createElement(MarkdownAnswer, { text: `![x](${dir}/a.html)` })),
    /<img src="\/stage\/chats\/c\/turns\/op-1\/artifacts\/a.html" alt="x"\/>/,
  );
});

test("a version the panel moves reaches every inline copy of that artifact", () => {
  const seen = [];
  const stop = onArtifactVersionChange((id) => seen.push(id));
  announceArtifactVersionChange("a");
  stop();
  announceArtifactVersionChange("b");
  assert.deepEqual(seen, ["a"]);
});
