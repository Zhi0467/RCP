import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
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
const { branchExperimentPollingKey, exactRunExperimentSelectionHref, parseProjectHash } =
  await server.ssrLoadModule("/src/experimentBoard.ts");
const { reduceExperimentSelection } = await server.ssrLoadModule("/src/hooks/useGraphSelection.ts");
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

test("leaving Runs and returning preserves the exact branch Experiment identity", () => {
  const route = {
    experiment_id: "experiment/shared",
    episode_id: "child-episode",
    graph_target: { kind: "branch", branch_id: "parent-episode" },
    parent_episode_id: "parent-episode",
  };
  const selected = reduceExperimentSelection(
    {
      selectedExperimentRunId: null,
      focusExperimentRunId: null,
      selectedExperimentRoute: null,
      selectedAutoResearchEpisodeId: null,
    },
    {
      kind: "route",
      experimentId: route.experiment_id,
      experimentRoute: route,
      autoResearchEpisodeId: null,
    },
  );
  const away = reduceExperimentSelection(selected, { kind: "view_changed" });
  const returned = reduceExperimentSelection(away, { kind: "view_changed" });

  assert.strictEqual(away, selected);
  assert.strictEqual(returned, selected);
  assert.equal(
    branchExperimentPollingKey("project-one", returned.selectedExperimentRoute),
    '["project-one","experiment/shared","child-episode","parent-episode","parent-episode"]',
  );
});

test("collapsing and re-expanding keeps an exact branch Experiment URL", () => {
  const route = {
    experiment_id: "experiment/shared",
    episode_id: "child-episode",
    graph_target: { kind: "branch", branch_id: "parent-episode" },
    parent_episode_id: "parent-episode",
  };
  const selected = {
    selectedExperimentRunId: route.experiment_id,
    focusExperimentRunId: route.experiment_id,
    selectedExperimentRoute: route,
    selectedAutoResearchEpisodeId: null,
  };
  const collapsed = reduceExperimentSelection(selected, {
    kind: "select",
    experimentId: null,
  });
  const reexpanded = reduceExperimentSelection(collapsed, {
    kind: "select",
    experimentId: route.experiment_id,
  });

  assert.deepEqual(reexpanded.selectedExperimentRoute, route);
  assert.deepEqual(
    parseProjectHash(
      exactRunExperimentSelectionHref(
        "project-one",
        reexpanded.selectedExperimentRunId,
        reexpanded.selectedExperimentRoute,
        reexpanded.selectedAutoResearchEpisodeId,
      ),
    ).experimentRoute,
    route,
  );
});

test("selecting another card replaces an old exact branch Experiment URL", () => {
  const oldRoute = {
    experiment_id: "experiment/old",
    episode_id: "child-episode",
    graph_target: { kind: "branch", branch_id: "parent-episode" },
    parent_episode_id: "parent-episode",
  };
  const href = exactRunExperimentSelectionHref("project-one", "experiment/new", oldRoute, null);

  assert.deepEqual(parseProjectHash(href), {
    projectId: "project-one",
    view: "execution",
    projectViewSpecified: true,
    experimentId: "experiment/new",
    experimentRoute: null,
    autoResearchEpisodeId: null,
  });
});

test("selecting an Experiment replaces an exact Auto-research episode URL", () => {
  const href = exactRunExperimentSelectionHref(
    "project-one",
    "experiment/new",
    null,
    "auto-research-episode",
  );

  assert.deepEqual(parseProjectHash(href), {
    projectId: "project-one",
    view: "execution",
    projectViewSpecified: true,
    experimentId: "experiment/new",
    experimentRoute: null,
    autoResearchEpisodeId: null,
  });
});

test("showing an Experiment replaces an exact Auto-research episode URL", () => {
  const route = parseProjectHash(
    "#/projects/project-one?view=runs&mode=auto_research&episode=completed-episode",
  );
  const selected = reduceExperimentSelection(
    {
      selectedExperimentRunId: null,
      focusExperimentRunId: null,
      selectedExperimentRoute: null,
      selectedAutoResearchEpisodeId: null,
    },
    {
      kind: "route",
      experimentId: route.experimentId,
      experimentRoute: route.experimentRoute,
      autoResearchEpisodeId: route.autoResearchEpisodeId,
    },
  );
  const experimentId = "experiment/new";
  const href = exactRunExperimentSelectionHref(
    route.projectId,
    experimentId,
    selected.selectedExperimentRoute,
    selected.selectedAutoResearchEpisodeId,
    "show",
  );
  const shown = reduceExperimentSelection(selected, { kind: "show", experimentId });

  assert.deepEqual(parseProjectHash(href), {
    projectId: "project-one",
    view: "execution",
    projectViewSpecified: true,
    experimentId,
    experimentRoute: null,
    autoResearchEpisodeId: null,
  });
  assert.deepEqual(shown, {
    selectedExperimentRunId: experimentId,
    focusExperimentRunId: experimentId,
    selectedExperimentRoute: null,
    selectedAutoResearchEpisodeId: null,
  });
});

test("showing an Experiment replaces its exact branch Experiment URL", () => {
  const route = parseProjectHash(
    "#/projects/project-one?view=runs&experiment=experiment%2Fshared&episode=child-episode&target=branch&branch=parent-episode&parent=parent-episode",
  );
  const selected = reduceExperimentSelection(
    {
      selectedExperimentRunId: null,
      focusExperimentRunId: null,
      selectedExperimentRoute: null,
      selectedAutoResearchEpisodeId: null,
    },
    {
      kind: "route",
      experimentId: route.experimentId,
      experimentRoute: route.experimentRoute,
      autoResearchEpisodeId: route.autoResearchEpisodeId,
    },
  );
  const href = exactRunExperimentSelectionHref(
    route.projectId,
    route.experimentId,
    selected.selectedExperimentRoute,
    selected.selectedAutoResearchEpisodeId,
    "show",
  );
  const shown = reduceExperimentSelection(selected, {
    kind: "show",
    experimentId: route.experimentId,
  });

  assert.deepEqual(parseProjectHash(href), {
    projectId: "project-one",
    view: "execution",
    projectViewSpecified: true,
    experimentId: "experiment/shared",
    experimentRoute: null,
    autoResearchEpisodeId: null,
  });
  assert.deepEqual(shown, {
    selectedExperimentRunId: "experiment/shared",
    focusExperimentRunId: "experiment/shared",
    selectedExperimentRoute: null,
    selectedAutoResearchEpisodeId: null,
  });
});

test("tab restoration retains the branch target instead of resolving the node id on main", () => {
  const route = {
    experiment_id: "experiment/shared",
    episode_id: "child-episode",
    graph_target: { kind: "branch", branch_id: "parent-episode" },
    parent_episode_id: "parent-episode",
  };
  const restored = reduceExperimentSelection(
    {
      selectedExperimentRunId: "experiment/main",
      focusExperimentRunId: null,
      selectedExperimentRoute: null,
      selectedAutoResearchEpisodeId: null,
    },
    {
      kind: "restore",
      experimentId: route.experiment_id,
      focusExperimentId: null,
      experimentRoute: route,
      autoResearchEpisodeId: null,
    },
  );

  assert.deepEqual(restored, {
    selectedExperimentRunId: route.experiment_id,
    focusExperimentRunId: null,
    selectedExperimentRoute: route,
    selectedAutoResearchEpisodeId: null,
  });
  assert.notStrictEqual(restored.selectedExperimentRoute, route);
  assert.equal(
    branchExperimentPollingKey("project-one", restored.selectedExperimentRoute),
    '["project-one","experiment/shared","child-episode","parent-episode","parent-episode"]',
  );
});

test("Runs routing and tab selection retain an exact Auto-research episode", () => {
  const route = parseProjectHash(
    "#/projects/project-one?view=runs&mode=auto_research&episode=completed-episode",
  );
  const selected = reduceExperimentSelection(
    {
      selectedExperimentRunId: null,
      focusExperimentRunId: null,
      selectedExperimentRoute: null,
      selectedAutoResearchEpisodeId: null,
    },
    {
      kind: "route",
      experimentId: route.experimentId,
      experimentRoute: route.experimentRoute,
      autoResearchEpisodeId: route.autoResearchEpisodeId,
    },
  );

  assert.equal(route.autoResearchEpisodeId, "completed-episode");
  assert.deepEqual(selected, {
    selectedExperimentRunId: null,
    focusExperimentRunId: null,
    selectedExperimentRoute: null,
    selectedAutoResearchEpisodeId: "completed-episode",
  });
  assert.strictEqual(reduceExperimentSelection(selected, { kind: "view_changed" }), selected);
});

test("an explicit malformed Runs route clears a cached exact branch selection", () => {
  const cachedRoute = {
    experiment_id: "experiment/shared",
    episode_id: "child-episode",
    graph_target: { kind: "branch", branch_id: "parent-episode" },
    parent_episode_id: "parent-episode",
  };
  const cached = reduceExperimentSelection(
    {
      selectedExperimentRunId: null,
      focusExperimentRunId: null,
      selectedExperimentRoute: null,
      selectedAutoResearchEpisodeId: null,
    },
    {
      kind: "restore",
      experimentId: cachedRoute.experiment_id,
      focusExperimentId: null,
      experimentRoute: cachedRoute,
      autoResearchEpisodeId: null,
    },
  );
  const explicitMalformedRoute = parseProjectHash(
    "#/projects/project-one?view=runs&experiment=experiment%2Fshared&episode=child-episode&target=branch",
  );
  const restored = explicitMalformedRoute.projectViewSpecified
    ? reduceExperimentSelection(cached, {
        kind: "route",
        experimentId: explicitMalformedRoute.experimentId,
        experimentRoute: explicitMalformedRoute.experimentRoute,
        autoResearchEpisodeId: explicitMalformedRoute.autoResearchEpisodeId,
      })
    : cached;

  assert.equal(explicitMalformedRoute.view, "execution");
  assert.equal(explicitMalformedRoute.experimentId, null);
  assert.equal(explicitMalformedRoute.experimentRoute, null);
  assert.deepEqual(restored, {
    selectedExperimentRunId: null,
    focusExperimentRunId: null,
    selectedExperimentRoute: null,
    selectedAutoResearchEpisodeId: null,
  });
  assert.equal(parseProjectHash("#/projects/project-one").projectViewSpecified, false);
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

test("the index shortcut returns to this space's own index, never out of the space", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const returnToProjects = source.slice(
    source.indexOf("const returnToProjects = () => {"),
    source.indexOf("const exitTeamSpace = () => {"),
  );

  // Cmd+T means the same thing in a team space as in a personal one: this
  // space's project index. Leaving the space is the separate Exit control, so
  // returning to projects must not reach for the local backend.
  assert.ok(returnToProjects.length > 0);
  assert.doesNotMatch(returnToProjects, /returnDesktopToPersonal|space_kind/);
  assert.match(returnToProjects, /returnToProjectIndex\(\)/);
  assert.match(source, /const exitTeamSpace = \(\) => \{\s*void returnDesktopToPersonal\(\)/);
});
