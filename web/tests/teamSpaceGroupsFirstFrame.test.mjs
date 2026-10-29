import assert from "node:assert/strict";
import { after, test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

const connection = {
  connection_id: "11111111-1111-4111-8111-111111111111",
  display_name: "Causal Systems Lab",
  ssh_target: "rcp@lab-server",
  remote_loopback_port: 8421,
  expected_space_id: "22222222-2222-4222-8222-222222222222",
  local_origin: "https://rcp-11111111111141118111111111111111.rcp.localhost:18421",
  last_known_cards: [],
  operator_route: null,
};

// The desktop host answers the saved-team listing before the index renders.
globalThis.window = {
  __TAURI_INTERNALS__: {
    invoke: async (command) => {
      assert.equal(command, "desktop_list_team_connections");
      return [connection];
    },
  },
};

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
const { TeamSpaceGroups } = await server.ssrLoadModule("/src/projects/TeamSpaceGroups.tsx");

after(async () => {
  delete globalThis.window;
  await server.close();
});

test("the index paints its saved team rows on its first frame", async () => {
  await new Promise((resolve) => setTimeout(resolve, 0));
  const html = renderToStaticMarkup(
    React.createElement(TeamSpaceGroups, { addOpen: false, onOpenAdd() {}, onCloseAdd() {} }),
  );

  assert.match(html, /class="team-space-group checking"/);
});
