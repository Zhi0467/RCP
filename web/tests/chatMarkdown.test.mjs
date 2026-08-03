import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { MarkdownAnswer } from "../src/chatMarkdown.ts";

test("chat Markdown renders formatting and unknown fenced languages as inert code", () => {
  const rendered = renderToStaticMarkup(
    MarkdownAnswer({
      text: '**Result**\n\n```made-up-language\n<widget onclick="run()">\n```',
    }),
  );

  assert.match(rendered, /<strong>Result<\/strong>/);
  assert.match(rendered, /<code class="language-made-up-language">/);
  assert.match(rendered, /&lt;widget onclick=&quot;run\(\)&quot;&gt;/);
});

test("chat Markdown does not execute raw HTML", () => {
  const rendered = renderToStaticMarkup(
    MarkdownAnswer({
      text: "<script>globalThis.compromised = true</script>",
    }),
  );

  assert.doesNotMatch(rendered, /<script>/);
  assert.match(rendered, /&lt;script&gt;/);
});

test("chat Markdown links exact graph node ids in prose and inline code", () => {
  const rendered = renderToStaticMarkup(
    MarkdownAnswer({
      text: [
        "See exp/known alongside exp/not-in-the-graph.",
        "",
        "`exp/known` keeps its code styling and becomes a node link.",
        "",
        "```text",
        "exp/known",
        "```",
        "",
        "[A web link](https://example.test/exp/known) and **exp/known**.",
      ].join("\n"),
      nodes: { "exp/known": { id: "exp/known" } },
      onOpenNode() {},
    }),
  );

  assert.match(rendered, /href="#rcp-node=exp%2Fknown"/);
  assert.match(rendered, /class="chat-node-reference"/);
  assert.match(rendered, /aria-label="Open node exp\/known"/);
  assert.match(rendered, /exp\/not-in-the-graph/);
  assert.doesNotMatch(rendered, /rcp-node=exp%2Fnot-in-the-graph/);
  assert.match(
    rendered,
    /<a href="#rcp-node=exp%2Fknown" class="chat-node-reference" aria-label="Open node exp\/known"><code>exp\/known<\/code><\/a>/,
  );
  assert.match(rendered, /<pre><code class="language-text">exp\/known\n<\/code><\/pre>/);
  assert.match(rendered, /href="https:\/\/example\.test\/exp\/known"/);
});
