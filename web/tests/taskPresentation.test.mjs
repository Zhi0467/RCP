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
const { useAgentTasks } = await server.ssrLoadModule("/src/agents/useAgentTasks.ts");
const { RefreshRuntimeContext } = await server.ssrLoadModule("/src/core/useResource.ts");
const { ResourceStore } = await server.ssrLoadModule("/src/core/resourceStore.ts");
const { taskStatusProfile } = await server.ssrLoadModule("/src/agents/taskResources.ts");
const { beginTaskPublication } = await server.ssrLoadModule("/src/agents/taskActions.ts");

test("presenting a late command response cannot overwrite an accepted task read", async () => {
  const store = new ResourceStore();
  const latest = { operation_id: "task", status: "succeeded", updated_at: "2" };
  const id = store.register("tab", { ...taskStatusProfile("p"), fetch: async () => [latest] });
  await new Promise(setImmediate);
  let actions;
  function Harness() {
    actions = useAgentTasks({ projectId: "p" });
    return null;
  }
  renderToStaticMarkup(
    React.createElement(
      RefreshRuntimeContext.Provider,
      {
        value: { store },
      },
      React.createElement(Harness),
    ),
  );
  for (const present of [actions.presentTask, actions.recordStartedTask]) {
    const response = { ...latest, status: "running", updated_at: "1" };
    const mutation = beginTaskPublication(store, "p");
    store.invalidate(id);
    await new Promise(setImmediate);
    mutation.settle(response);
    present(response);
    assert.deepEqual(store.committedValue(id), [latest]);
  }
});
