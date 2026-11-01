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
const { ChatComposer } = await server.ssrLoadModule("/src/chat/ChatComposer.tsx");
const { useComposerAttachments } = await server.ssrLoadModule("/src/chat/composerAttachments.tsx");
const comments = { annotations: [], ui: {} };
const props = {
  mode: "work",
  comments,
  field: React.createElement("textarea", { "aria-label": "Message" }),
  tools: React.createElement("button", null, "Voice"),
  actions: React.createElement("button", null, "Send"),
};

test("the composer accepts caller hints without project, attachments, or skills", () => {
  const html = renderToStaticMarkup(
    React.createElement(ChatComposer, {
      ...props,
      hint: React.createElement("span", { "data-hint": "caller" }),
    }),
  );
  assert.match(html, /data-hint="caller"/);
  assert.match(html, /<textarea/);
  assert.equal((html.match(/<button/g) ?? []).length, 2);
  assert.doesNotMatch(html, /type="file"/);
});

for (const project of [{ id: "p" }, null]) {
  test(`attachment composer needs ${project ? "only a project id" : "only a Space collection"}`, () => {
    let state;
    function Harness() {
      state = useComposerAttachments({
        project,
        attachmentsUrl: project ? undefined : "/api/agent-threads/t/attachments",
        scopeId: "t",
        referencesKey: null,
        input: { current: null },
        message: { get: () => "", set: () => {} },
        onError: () => {},
      });
      return React.createElement(ChatComposer, { ...props, composer: state });
    }
    const html = renderToStaticMarkup(React.createElement(Harness));
    assert.match(html, /type="file"/);
    assert.equal(state.ui.referencesDisabled, !project);
    assert.deepEqual(state.turnFields(), {});
  });
}
