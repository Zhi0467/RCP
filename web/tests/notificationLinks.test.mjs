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
const {
  episodeNotificationHash,
  graphNotificationHash,
  isNotificationLinkHash,
  parseNotificationLink,
} = await server.ssrLoadModule("/src/core/notificationLinks.ts");
const { parseProjectHash } = await server.ssrLoadModule("/src/experiments/experimentBoardModel.ts");

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
  const consolidation = parseNotificationLink("#/projects/p/targets/main/consolidation/run-1");
  assert.equal(consolidation.kind, "consolidation");
  assert.equal(consolidation.itemId, "run-1");
  assert.equal(graphNotificationHash(consolidation), "#/projects/p?view=attention");

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

test("an Auto-research run opens on its own branch", () => {
  const link = parseNotificationLink("#/projects/p/targets/branch%3Ab1/episode/b1");
  const hash = episodeNotificationHash(
    link,
    { episode_id: "b1", mode: "auto_research", graph_target: { kind: "branch", branch_id: "b1" } },
    [],
  );
  const params = new URLSearchParams(hash.split("?")[1]);
  assert.equal(parseProjectHash(hash).autoResearchEpisodeId, "b1");
  assert.equal(params.get("branch_id"), "b1");
});

test("a notification link while a project is open is left to the resolver", () => {
  // As a project route it would name no project and close the open one.
  const hash = "#/projects/p/targets/branch%3Ab1/episode/e1";
  assert.equal(parseProjectHash(hash).projectId, null);
  assert.equal(isNotificationLinkHash(hash), true);
  for (const route of ["#/projects/p", "#/projects/p?view=chats&chat=b1&branch_id=b1"]) {
    assert.equal(isNotificationLinkHash(route), false, route);
  }
});
