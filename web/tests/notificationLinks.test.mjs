import assert from "node:assert/strict";
import { after, test } from "node:test";
import { createServer } from "vite";

const server = await createServer({
  root: new URL("..", import.meta.url).pathname,
  configFile: false,
  logLevel: "silent",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true },
});
after(() => server.close());
const { episodeNotificationHash, graphNotificationHash, parseNotificationLink } =
  await server.ssrLoadModule("/src/notificationLinks.ts");

test("notification links parse exactly and refuse other hashes", () => {
  assert.deepEqual(parseNotificationLink("#/projects/p%201/targets/main/proposal/prop%2Fa"), {
    projectId: "p 1",
    target: "main",
    kind: "proposal",
    itemId: "prop/a",
  });
  for (const hash of [
    "#/projects/p",
    "#/projects/p/targets/main/note/n",
    "#/projects/p/targets/main/proposal/a?view=dag",
    "#/projects/p/targets/main/proposal/%E0%A4",
  ]) {
    assert.equal(parseNotificationLink(hash), null, hash);
  }
});

test("graph items open the Inbox and episodes their exact run", () => {
  const graph = parseNotificationLink("#/projects/p/targets/main/blocker/b");
  assert.equal(graphNotificationHash(graph), "#/projects/p?view=attention");

  const link = parseNotificationLink("#/projects/p/targets/main/episode/e1");
  assert.equal(
    episodeNotificationHash(link, { episode_id: "e1", mode: "auto_research" }, []),
    "#/projects/p?view=runs&mode=auto_research&episode=e1",
  );
  const entry = {
    graph_target: { kind: "main" },
    parent_episode_id: null,
    node: { id: "exp" },
    episode: { episode_id: "e1", mode: "experiment_loop", control_node_id: "exp" },
  };
  const exact = episodeNotificationHash(link, entry.episode, [entry]);
  assert.match(exact, /view=runs&experiment=exp&episode=e1&target=main/);
  assert.equal(episodeNotificationHash(link, null, []), "#/projects/p?view=runs");
});
