import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

import { listMachineDirectory, updateSpaceMachine } from "../src/api.ts";
import {
  EMPTY_PATH_PICKER,
  applyDirectoryPage,
  canPickFolder,
  directoryRequest,
  filterMoveIsCurrent,
  pathBreadcrumbs,
} from "../src/pathPicker.ts";
import {
  createPathEditor,
  projectMachineAlias,
  setupMachineSelection,
  spaceMachineForProject,
  writablePathsRequest,
} from "../src/spaceMachines.ts";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { SpaceSettings } = await server.ssrLoadModule("/src/views/SpaceSettings.tsx");
const { ProjectSettings } = await server.ssrLoadModule("/src/views/ProjectSettings.tsx");
const { MachineCard } = await server.ssrLoadModule("/src/components/MachineCard.tsx");
const { LandingIdentityMenu } = await server.ssrLoadModule(
  "/src/components/LandingIdentityMenu.tsx",
);

after(() => server.close());

const projectId = "11111111-1111-4111-8111-111111111111";
const gpu = {
  machine_id: "m-gpu",
  name: "GPU",
  host: "alice@gpu.example",
  os_account: "alice",
  writable_paths: ["/data/cache", "/scratch"],
  hidden_folders: ["/private"],
  projects: [{ project_id: projectId, project_name: "Project", alias: "gpu" }],
  in_use: true,
};

function page(entries, nextOffset, path = "/data", parent = "/") {
  return { path, parent, entries, total: 3, next_offset: nextOffset };
}
const folder = (name, path = `/data/${name}`) => ({ name, path, protected: false });

async function captureFetch(run, response = {}) {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (path, init) => {
    requests.push({ path, method: init?.method, body: init?.body ? JSON.parse(init.body) : null });
    return new Response(JSON.stringify(response), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
  return requests;
}

test("the picker pages one folder, keeps its filter across pages, and resets both on open", () => {
  const first = { kind: "open", path: null };
  assert.deepEqual(directoryRequest(EMPTY_PATH_PICKER, first), { path: null });
  let state = applyDirectoryPage(EMPTY_PATH_PICKER, first, page([folder("a"), folder("b")], 2));
  assert.equal(state.entries.length, 2);
  assert.equal(state.nextOffset, 2);

  const more = { kind: "more" };
  assert.deepEqual(directoryRequest(state, more), { path: "/data", offset: 2 });
  state = applyDirectoryPage(state, more, page([folder("c")], null));
  assert.deepEqual(
    state.entries.map((entry) => entry.name),
    ["a", "b", "c"],
  );
  assert.equal(state.nextOffset, null);

  const narrow = { kind: "filter", filter: " hug ", path: "/data" };
  assert.deepEqual(directoryRequest(state, narrow), { path: "/data", filter: "hug" });
  state = applyDirectoryPage(state, narrow, page([folder("hug")], 1));
  assert.equal(state.entries.length, 1);
  assert.deepEqual(directoryRequest(state, more), { path: "/data", filter: "hug", offset: 1 });
  state = applyDirectoryPage(state, more, page([folder("hug2")], null));
  assert.equal(state.entries.length, 2);

  const open = { kind: "open", path: "/data/hug" };
  assert.deepEqual(directoryRequest(state, open), { path: "/data/hug" });
  state = applyDirectoryPage(state, open, page([], null, "/data/hug", "/data"));
  assert.equal(state.filter, "");
  assert.equal(state.entries.length, 0);
  assert.equal(state.parent, "/data");
});

test("breadcrumbs name each ancestor from the root", () => {
  assert.deepEqual(
    pathBreadcrumbs("/data/shared/huggingface").map((crumb) => crumb.path),
    ["/", "/data", "/data/shared", "/data/shared/huggingface"],
  );
  assert.equal(pathBreadcrumbs("/").length, 1);
});

test("a writable-path edit sends the whole list to the machine record", async () => {
  assert.deepEqual(writablePathsRequest(gpu, { kind: "add", path: "/data/hf" }), {
    writable_paths: ["/data/cache", "/scratch", "/data/hf"],
  });
  assert.deepEqual(writablePathsRequest(gpu, { kind: "add", path: "/scratch" }), {
    writable_paths: ["/data/cache", "/scratch"],
  });
  assert.deepEqual(writablePathsRequest(gpu, { kind: "remove", path: "/data/cache" }), {
    writable_paths: ["/scratch"],
  });

  const requests = await captureFetch(() =>
    updateSpaceMachine(gpu.machine_id, writablePathsRequest(gpu, { kind: "remove", path: "/x" })),
  );
  assert.deepEqual(requests, [
    {
      path: "/api/space/machines/m-gpu",
      method: "PATCH",
      body: { writable_paths: ["/data/cache", "/scratch"] },
    },
  ]);
});

test("the folder listing is one POST to the machine's directories endpoint", async () => {
  const requests = await captureFetch(() =>
    listMachineDirectory("m-gpu", { path: "/data", filter: "hf", offset: 50 }),
  );
  assert.deepEqual(requests, [
    {
      path: "/api/space/machines/m-gpu/directories",
      method: "POST",
      body: { path: "/data", filter: "hf", offset: 50 },
    },
  ]);
});

test("a project machine finds its space record by project and alias", () => {
  assert.equal(spaceMachineForProject([gpu], projectId, { alias: "gpu" }), gpu);
  assert.equal(spaceMachineForProject([gpu], projectId, { alias: "local" }), null);
  assert.equal(spaceMachineForProject([gpu], "other", { alias: "gpu" }), null);
});

test("both Settings levels render the same machine card with one remove per path", () => {
  const render = (level) =>
    renderToStaticMarkup(
      React.createElement(MachineCard, {
        title: "gpu",
        hostLabel: gpu.host,
        osAccount: gpu.os_account,
        record: gpu,
        level,
        onRecordChange() {},
      }),
    );
  for (const level of ["space", "project"]) {
    const html = render(level);
    assert.match(html, new RegExp(`data-machine-writable-paths="${level}"`));
    assert.equal(html.match(/data-machine-action="remove-path"/g)?.length, 2);
    assert.equal(html.match(/data-machine-action="add-path"/g)?.length, 1);
  }
  // The card's own name is renamed on the space page; a project names its machines itself.
  assert.equal(render("space").match(/data-machine-action="rename"/g)?.length, 1);
  assert.equal(render("project").match(/data-machine-action="rename"/g), null);
  // Only the project card carries the applies-to-every-project note.
  assert.equal(
    render("project").match(/machine-writable-note/g)?.length,
    (render("space").match(/machine-writable-note/g)?.length ?? 0) + 1,
  );
});

function settingsProject() {
  const profile = { provider: "codex", model: "", reasoning: "medium", run_on: "local" };
  const metric = {
    bytes: 0,
    count: 0,
    limits: { max_bytes: 1, max_count: 1, ttl_seconds: 1 },
    reclaimable_bytes: 0,
    reclaimable_count: 0,
  };
  return {
    id: projectId,
    name: "Project",
    state_repository: "research",
    run_on: "local",
    default_run_truth_scope: ["research"],
    default_auto_research_invocation_ceiling: 10,
    repositories: [{ alias: "research", machine: "local", path: "/repo" }],
    machines: [
      {
        alias: "local",
        host: "",
        os_account: "",
        provider_paths: { codex: "codex" },
        compute_probes: { scheduler: null, helper: null },
      },
    ],
    agent_profiles: Object.fromEntries(
      ["seed", "refresh", "node_chat", "project_chat", "paper_coach", "orchestrator"].map((id) => [
        id,
        profile,
      ]),
    ),
    providers: {},
    provider_readiness: {},
    cache_metrics: { remote_sources: metric, session_slices: metric },
  };
}

const sectionClasses = (html) =>
  [
    "server-settings",
    "space-machine-settings",
    "provider-login-settings",
    "transcription-settings",
    "space-cache-settings",
    "display-settings",
    "provider-path-settings",
    "compute-settings",
    "boundary-settings",
    "skill-settings",
    "agent-defaults",
    "cache-settings",
  ].filter((name) => new RegExp(`class="[^"]*\\b${name}\\b`).test(html));

test("space-wide sections render on space Settings and project sections on project Settings", () => {
  const space = (spaceKind) =>
    renderToStaticMarkup(
      React.createElement(SpaceSettings, {
        spaceKind,
        cacheProjectId: projectId,
        cacheClearDisabled: false,
        onAllCachesCleared() {},
        onClose() {},
      }),
    );
  assert.deepEqual(sectionClasses(space("team")), [
    "server-settings",
    "space-machine-settings",
    "provider-login-settings",
    "transcription-settings",
    "provider-path-settings",
  ]);
  assert.deepEqual(sectionClasses(space("personal")), [
    "space-machine-settings",
    "provider-login-settings",
    "transcription-settings",
    "space-cache-settings",
    "provider-path-settings",
    "cache-settings",
  ]);

  for (const spaceKind of ["personal", "team"]) {
    const html = renderToStaticMarkup(
      React.createElement(ProjectSettings, {
        apiBase: `/api/projects/${projectId}`,
        project: settingsProject(),
        identity: null,
        onLeftProject() {},
        usage: null,
        onRefreshUsage: async () => {},
        cacheClearDisabled: false,
        onSaved() {},
        onCacheMetricsChange() {},
        onRefreshReadiness: async () => {},
        spaceKind,
      }),
    );
    assert.match(html, /data-settings-level="project"/);
    assert.deepEqual(sectionClasses(html), [
      "provider-path-settings",
      "compute-settings",
      "boundary-settings",
      "skill-settings",
      "agent-defaults",
      "cache-settings",
    ]);
    assert.match(html, /data-machine-action="add-machine"/);
    assert.doesNotMatch(html, /app-cache-danger-row/);
  }
});

test("the identity menu holds Display, and Space settings sits beside it", () => {
  const identity = {
    space_id: "space",
    space_kind: "personal",
    user: {
      user_id: "user",
      display_name: "Ada",
      identity_kind: "local_owner",
      created_at: "2026-09-27T00:00:00Z",
      updated_at: "2026-09-27T00:00:00Z",
    },
  };
  const html = renderToStaticMarkup(
    React.createElement(LandingIdentityMenu, {
      identity,
      identityError: null,
      onRequestName() {},
      appearance: {
        themeChoice: "classic",
        colorModeChoice: "system",
        onThemeChoiceChange() {},
        onColorModeChoiceChange() {},
      },
      textScale: { value: 100, onChange() {} },
    }),
  );
  assert.doesNotMatch(html, /data-identity-action="space-settings"/);
  assert.match(html, /class="appearance-picker"/);
  assert.match(html, /class="text-scale-controls"/);
});

test("a filter typed in one folder never runs after the picker opens another", () => {
  const inData = { ...EMPTY_PATH_PICKER, path: "/data" };
  const typed = { kind: "filter", filter: "hf", path: "/data" };
  assert.equal(filterMoveIsCurrent(inData, typed), true);
  assert.equal(filterMoveIsCurrent({ ...inData, path: "/data/child" }, typed), false);
  // The request names the folder the filter was typed in, not wherever the state is now.
  assert.deepEqual(directoryRequest({ ...inData, path: "/data/child" }, typed), {
    path: "/data",
    filter: "hf",
  });
  assert.equal(filterMoveIsCurrent(inData, { kind: "open", path: "/x" }), true);
});

test("choosing a folder waits for a pending navigation or pick", () => {
  const settled = { ...EMPTY_PATH_PICKER, path: "/data" };
  assert.equal(canPickFolder(settled, { navigating: false, picking: false }), true);
  assert.equal(canPickFolder(settled, { navigating: true, picking: false }), false);
  assert.equal(canPickFolder(settled, { navigating: false, picking: true }), false);
  assert.equal(canPickFolder(EMPTY_PATH_PICKER, { navigating: false, picking: false }), false);
  // The root can be browsed but never granted.
  const root = { ...EMPTY_PATH_PICKER, path: "/" };
  assert.equal(canPickFolder(root, { navigating: false, picking: false }), false);
});

test("path edits run one at a time and each starts from the last saved list", async () => {
  const sent = [];
  let finish;
  const editor = createPathEditor((machineId, request) => {
    sent.push({ machineId, request });
    return new Promise((resolve) => {
      finish = () => resolve({ ...gpu, writable_paths: request.writable_paths });
    });
  });
  editor.sync(gpu);
  const removal = editor.edit({ kind: "remove", path: "/data/cache" });
  assert.equal(editor.pending, true);
  await assert.rejects(editor.edit({ kind: "add", path: "/data/hf" }));
  // A stale record handed down mid-edit does not replace the edit's own answer.
  editor.sync(gpu);
  finish();
  await removal;
  assert.equal(editor.pending, false);

  const addition = editor.edit({ kind: "add", path: "/data/hf" });
  finish();
  await addition;
  assert.deepEqual(
    sent.map((item) => item.request.writable_paths),
    [["/scratch"], ["/scratch", "/data/hf"]],
  );
});

test("setup keeps the chosen card when two accounts share a host", () => {
  const bob = { ...gpu, machine_id: "m-bob", os_account: "bob", projects: [] };
  const shared = { ...gpu, host: "gpu.example" };
  const sharedBob = { ...bob, host: "gpu.example" };
  const cards = [shared, sharedBob];
  assert.equal(setupMachineSelection(cards, "m-bob", "gpu.example"), sharedBob);
  assert.equal(setupMachineSelection(cards, "m-gpu", "gpu.example"), shared);
  // A host alone cannot tell the two apart.
  assert.equal(setupMachineSelection(cards, null, "gpu.example"), null);
  // One card on a host is found by host, and a changed host drops a stale choice.
  assert.equal(setupMachineSelection([gpu, sharedBob], "m-bob", gpu.host), gpu);
});

test("adding a machine names it from its card, numbered past taken names", () => {
  assert.equal(projectMachineAlias("Lab cluster", ["laptop"]), "lab-cluster");
  assert.equal(
    projectMachineAlias("Lab cluster", ["lab-cluster", "lab-cluster-2"]),
    "lab-cluster-3",
  );
  assert.ok(projectMachineAlias("x".repeat(80), ["x".repeat(48)]).length <= 48);
});

test("hidden folders serialize edits without changing defaults or writable paths", async () => {
  const calls = [];
  const editor = createPathEditor(async (id, request) => {
    calls.push({ id, request });
    return { ...gpu, ...request };
  }, "hidden_folders");
  editor.sync(gpu);
  await editor.edit({ kind: "add", path: "/credentials" });
  await editor.edit({ kind: "remove", path: "/private" });
  assert.deepEqual(calls, [
    { id: gpu.machine_id, request: { hidden_folders: ["/private", "/credentials"] } },
    { id: gpu.machine_id, request: { hidden_folders: ["/credentials"] } },
  ]);
});

for (const status of ["unhidden", "enforced", null]) {
  test(`hidden-folder defaults are read-only with readiness ${status}`, () => {
    const record = {
      ...gpu,
      hidden_read: {
        default_paths: ["~/default-secret"],
        user_folders: gpu.hidden_folders,
        readiness:
          status === null
            ? null
            : {
                status,
                reasons:
                  status === "unhidden"
                    ? [
                        "wrapper_unavailable",
                        "browser_unwrapped_macos",
                        "deploy_key_agent_unconfirmed",
                        "ssh_key_agent_unconfirmed",
                      ]
                    : [],
              },
      },
    };
    const html = renderToStaticMarkup(
      React.createElement(MachineCard, {
        title: "GPU",
        hostLabel: "GPU",
        osAccount: "worker",
        record,
        level: "space",
        onRecordChange() {},
      }),
    );
    assert.ok(html.includes(`data-hidden-read-status="${status ?? "unchecked"}"`));
    for (const reason of record.hidden_read.readiness?.reasons ?? []) {
      assert.ok(html.includes(`data-hidden-read-reason="${reason}"`));
    }
    const defaultRow = html.match(/<li[^>]*data-hidden-default=""[^>]*>(.*?)<\/li>/)?.[1];
    assert.ok(defaultRow);
    assert.doesNotMatch(defaultRow, /<button/);
    assert.equal((html.match(/data-machine-action="remove-hidden-folder"/g) ?? []).length, 1);
  });
}
