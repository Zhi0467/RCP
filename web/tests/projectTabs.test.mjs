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
const {
  adjacentProjectTabId,
  closeProjectTab,
  initialProjectHash,
  openProjectTab,
  projectTabShortcut,
  projectViewportRef,
} = await server.ssrLoadModule("/src/projectTabs.ts");
const { ProjectDock } = await server.ssrLoadModule("/src/components/ProjectDock.tsx");

after(() => server.close());

const alpha = { id: "alpha", name: "Alpha study" };
const beta = { id: "beta", name: "Beta study" };
const gamma = { id: "gamma", name: "Gamma study" };

test("opening appends once without reordering existing tabs", () => {
  const first = openProjectTab([alpha, beta], gamma);
  assert.deepEqual(first, [alpha, beta, gamma]);
  assert.strictEqual(openProjectTab(first, beta), first);
  assert.deepEqual(openProjectTab(first, { ...beta, name: "Beta renamed" }), [
    alpha,
    { id: "beta", name: "Beta renamed" },
    gamma,
  ]);
});

test("only a real page reload discards the initial project route", () => {
  const deepLink = "#/projects/alpha?view=runs&experiment=experiment-1";
  assert.equal(initialProjectHash(deepLink, "navigate"), deepLink);
  assert.equal(initialProjectHash(deepLink, "back_forward"), deepLink);
  assert.equal(initialProjectHash(deepLink, undefined), deepLink);
  assert.equal(initialProjectHash(deepLink, "reload"), "");
});

test("DAG viewport refs are stable and isolated by project", () => {
  const refs = new Map();
  const alphaRef = projectViewportRef(refs, "alpha");
  alphaRef.current = { zoom: 1.4, scrollLeft: 20, scrollTop: 30 };
  const betaRef = projectViewportRef(refs, "beta");
  betaRef.current = { zoom: 0.8, scrollLeft: 4, scrollTop: 7 };

  assert.strictEqual(projectViewportRef(refs, "alpha"), alphaRef);
  assert.deepEqual(alphaRef.current, { zoom: 1.4, scrollLeft: 20, scrollTop: 30 });
  assert.deepEqual(betaRef.current, { zoom: 0.8, scrollLeft: 4, scrollTop: 7 });
});

test("closing an inactive tab keeps the active project", () => {
  assert.deepEqual(closeProjectTab([alpha, beta, gamma], "alpha", "beta"), {
    tabs: [alpha, gamma],
    activeProjectId: "alpha",
  });
});

test("closing the active tab chooses right, then left, then index", () => {
  assert.equal(closeProjectTab([alpha, beta, gamma], "beta", "beta").activeProjectId, "gamma");
  assert.equal(closeProjectTab([alpha, beta], "beta", "beta").activeProjectId, "alpha");
  assert.equal(closeProjectTab([alpha], "alpha", "alpha").activeProjectId, null);
});

test("adjacent tab navigation wraps and starts from the index edge", () => {
  const tabs = [alpha, beta, gamma];
  assert.equal(adjacentProjectTabId(tabs, "alpha", -1), "gamma");
  assert.equal(adjacentProjectTabId(tabs, "gamma", 1), "alpha");
  assert.equal(adjacentProjectTabId(tabs, null, 1), "alpha");
  assert.equal(adjacentProjectTabId(tabs, null, -1), "gamma");
});

test("shortcuts require their exact modifiers and ignore editable targets", () => {
  assert.equal(
    projectTabShortcut(
      { key: "ArrowLeft", metaKey: true, altKey: true, ctrlKey: false, shiftKey: false },
      false,
    ),
    "previous",
  );
  assert.equal(
    projectTabShortcut(
      { key: "ArrowRight", metaKey: true, altKey: true, ctrlKey: false, shiftKey: false },
      true,
    ),
    null,
  );
  assert.equal(
    projectTabShortcut(
      { key: "t", metaKey: true, altKey: false, ctrlKey: false, shiftKey: false },
      true,
    ),
    "index",
  );
  assert.equal(
    projectTabShortcut(
      { key: "t", metaKey: true, altKey: true, ctrlKey: false, shiftKey: false },
      false,
    ),
    null,
  );
});

test("dock exposes current-page navigation and named close controls", () => {
  const html = renderToStaticMarkup(
    React.createElement(ProjectDock, {
      tabs: [alpha, beta],
      activeProjectId: "beta",
      onActivate() {},
      onClose() {},
    }),
  );
  assert.match(html, /aria-label="Open projects"/);
  assert.match(html, /aria-current="page"[^>]*title="Beta study"/);
  assert.doesNotMatch(html, /role="tab(list)?"|aria-selected=/);
  assert.match(html, /aria-label="Close Alpha study"/);
  assert.match(html, /aria-label="Close Beta study"/);
});
