import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";
const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const { QuestionCard } = await server.ssrLoadModule("/src/chat/QuestionCard.tsx");

test("a question renders its Markdown structure", () => {
  const html = renderToStaticMarkup(
    React.createElement(QuestionCard, {
      question: {
        question_id: "q",
        state: "pending",
        can_answer: true,
        withdrawn_readonly: false,
        agent_waiting: false,
        choices: [],
        multiple: false,
        question: "**Context**\n\n- first\n- second",
      },
      apiBase: "/api",
      onResolved() {},
    }),
  );
  assert.equal((html.match(/<li>/g) ?? []).length, 2);
  assert.match(html, /<strong>/);
});
