import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import { repositoryPickerPresentation, stateRepositoryAfterRemoval } from "../src/projectSetup.ts";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { RepositoryEditor } = await server.ssrLoadModule("/src/views/ProjectSetup.tsx");

after(() => server.close());

const repositories = [
  { id: 1, alias: "research" },
  { id: 2, alias: "analysis" },
  { id: 3, alias: "paper" },
];

test("removing the canonical repository selects the first remaining alias", () => {
  assert.equal(stateRepositoryAfterRemoval(repositories, 1, "research"), "analysis");
});

test("repository removal preserves another selection and handles an empty remainder", () => {
  assert.equal(stateRepositoryAfterRemoval(repositories, 2, "research"), "research");
  assert.equal(stateRepositoryAfterRemoval([repositories[0]], 1, "research"), "");
});

test("only desktop local repositories offer the native folder picker", () => {
  assert.deepEqual(repositoryPickerPresentation("local", true), {
    showPicker: true,
    hint: null,
  });
  assert.deepEqual(repositoryPickerPresentation("local", false), {
    showPicker: false,
    hint: "Paste an absolute path. Finder selection is available in the desktop app.",
  });
  assert.deepEqual(repositoryPickerPresentation("ssh", true), {
    showPicker: false,
    hint: null,
  });
  assert.deepEqual(repositoryPickerPresentation("ssh", false), {
    showPicker: false,
    hint: null,
  });
});

test("the repository path label targets only its input while the picker stays a sibling", () => {
  const originalWindow = globalThis.window;
  globalThis.window = { __TAURI_INTERNALS__: {} };
  try {
    const html = renderToStaticMarkup(
      React.createElement(RepositoryEditor, {
        repository: {
          id: 7,
          alias: "research",
          location: "local",
          path: "/Users/example/research",
          host: "",
          default_read: true,
        },
        canonical: true,
        only: true,
        onCanonical() {},
        onChange() {},
      }),
    );
    const labelStart = html.indexOf('<label for="repository-path-7">');
    const labelEnd = html.indexOf("</label>", labelStart);
    const inputStart = html.indexOf('id="repository-path-7"', labelEnd);
    const pickerStart = html.indexOf("Choose folder…", inputStart);

    assert.ok(labelStart >= 0);
    assert.ok(labelEnd > labelStart);
    assert.ok(inputStart > labelEnd);
    assert.ok(pickerStart > inputStart);
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});
