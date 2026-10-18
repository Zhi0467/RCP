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
const { BranchChangeDetail, ChangedFields } = await server.ssrLoadModule(
  "/src/graph/BranchChangeDetail.tsx",
);
const { MergeGraphCounts } = await server.ssrLoadModule("/src/experiments/EpisodeMergePanel.tsx");
after(() => server.close());
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));

test("created nodes have one tag and retain history without a field list", () => {
  const html = render(BranchChangeDetail, {
    change: {
      before: null,
      after: { id: "node/new", title: "example" },
      change: "created",
      history: [{ revision: 2, producer: "agent", summary: "entity change", task_id: "task/1" }],
    },
    onInspectTask() {},
  });
  assert.equal((html.match(/class="eyebrow"/g) ?? []).length, 1);
  assert.ok(!html.includes('class="branch-change-fields"'));
  assert.equal((html.match(/<li>/g) ?? []).length, 1);
  assert.equal((html.match(/<button/g) ?? []).length, 1);
});

test("edited fields omit internals and empty pairs unless the merge names them", () => {
  const before = {
    title: "old",
    type: "a",
    id: "node/1",
    current_summary_stale: false,
    extension_fields: {},
    source_refs: [],
    a: null,
    b: "",
    c: [],
    d: {},
  };
  const after = {
    title: "new",
    type: "b",
    id: "node/2",
    current_summary_stale: true,
    extension_fields: { x: 1 },
    source_refs: ["x"],
    a: [],
    b: {},
    c: null,
  };
  const fieldCount = (mergePaths) =>
    (
      render(ChangedFields, { before, after, mergePaths }).match(/class="branch-change-field"/g) ??
      []
    ).length;
  const path = (field_path, main) => ({ field_path, main, conflict: true });
  // Title and extension fields are content; type, id, and source refs are internal.
  assert.equal(fieldCount([path("title", "current")]), 2);
  assert.equal(fieldCount([path("source_refs", ["y"])]), 3);
  const html = render(ChangedFields, {
    before,
    after,
    mergePaths: [path("extension_fields", { x: 2 })],
  });
  assert.equal((html.match(/class="branch-diff-word diff-conflict"/g) ?? []).length, 1);
  assert.match(html, /class="branch-change-value main-now"><span>[^<]*<\/span> X: 2/);
});

test("a created node the merge must decide shows the competing main values", () => {
  const html = render(BranchChangeDetail, {
    change: {
      before: null,
      after: { id: "n", title: "branch", type: "b", source_refs: [] },
      change: "created",
      history: [],
    },
    mergePaths: [
      {
        field_path: "$",
        main: { id: "n", title: "main", type: "t", source_refs: ["ref"] },
        conflict: true,
      },
    ],
  });
  assert.ok(html.includes('class="branch-change-fields"'));
  // Title, type, and an empty branch value against main's show; the shared id stays hidden.
  assert.equal((html.match(/class="branch-change-field"/g) ?? []).length, 3);
  assert.match(html, /class="branch-change-value main-now"><span>[^<]*<\/span> main/);
});

test("created edges and removed entities retain field values", () => {
  for (const props of [
    { before: null, after: { relation: "supports" } },
    { before: { title: "old" }, after: null },
  ]) {
    const html = render(ChangedFields, props);
    assert.equal((html.match(/class="branch-change-field"/g) ?? []).length, 1);
    assert.ok(html.includes('class="branch-change-value branch"'));
  }
});

test("presence conflicts include destination-only fields and unchanged branch values", () => {
  const html = render(ChangedFields, {
    before: { title: "same" },
    after: { title: "same" },
    mergePaths: [
      {
        field_path: "$",
        main: { title: "destination", body: "added" },
        conflict: true,
        needs_agent: true,
      },
    ],
  });
  assert.equal((html.match(/class="branch-change-field"/g) ?? []).length, 2);
  assert.equal((html.match(/class="branch-change-value main-now"/g) ?? []).length, 2);
});

test("review, conflict with review, and delivered paths retain their field badges", () => {
  for (const [flags, marks] of [
    [{ needs_agent: true, needs_proposal: true }, ["proposal"]],
    [{ needs_agent: true, needs_proposal: true, conflict: true }, ["conflict", "proposal"]],
    [{ delivered: true }, ["delivered"]],
  ]) {
    const html = render(ChangedFields, {
      before: { title: "before" },
      after: { title: "branch" },
      mergePaths: [{ field_path: "title", main: "destination", ...flags }],
    });
    assert.deepEqual(
      [...html.matchAll(/class="branch-diff-word diff-([^" ]+)"/g)].map((match) => match[1]),
      marks,
    );
  }
});

test("a delivered creation retains its merge mark", () => {
  const html = render(BranchChangeDetail, {
    change: { before: null, after: { title: "created" }, change: "created", history: [] },
    mergePaths: [{ field_path: "$", delivered: true }],
  });
  assert.equal((html.match(/class="branch-diff-word diff-delivered"/g) ?? []).length, 1);
});

test("an explicitly named nullable conflict stays visible", () => {
  const html = render(ChangedFields, {
    before: { body: null },
    after: { body: null },
    mergePaths: [{ field_path: "body", main: "destination", conflict: true, needs_agent: true }],
  });
  assert.equal((html.match(/class="branch-change-field"/g) ?? []).length, 1);
  assert.match(html, /class="branch-change-value main-now"><span>[^<]*<\/span> destination/);
});

test("merge summary uses server counts for connected reviews, not path or entity totals", () => {
  const html = render(MergeGraphCounts, {
    graph: {
      direct: 4,
      conflicts: 2,
      reviews: 1,
      paths: ["a", "b", "c"].map((id) => ({
        entity: "node",
        id,
        field_path: "title",
        needs_proposal: true,
        needs_agent: true,
      })),
    },
  });
  assert.deepEqual(
    [...html.matchAll(/class="branch-diff-word diff-([^" ]+)"[^>]*>(\d+)/g)].map((match) => [
      match[1],
      Number(match[2]),
    ]),
    [
      ["changed", 4],
      ["conflict", 2],
      ["proposal", 1],
    ],
  );
});
