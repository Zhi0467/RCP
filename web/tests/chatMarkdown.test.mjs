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
