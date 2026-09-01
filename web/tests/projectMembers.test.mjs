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
const { ProjectMembers, inviteUnavailableReason } = await server.ssrLoadModule(
  "/src/components/ProjectMembers.tsx",
);

after(() => server.close());

const ada = { user_id: "11111111-1111-4111-8111-111111111111", display_name: "Ada" };
const grace = { user_id: "22222222-2222-4222-8222-222222222222", display_name: "Grace" };

function identityIn(spaceKind) {
  return {
    space_id: "33333333-3333-4333-8333-333333333333",
    space_kind: spaceKind,
    space_name: spaceKind === "team" ? "Causal Systems Lab" : null,
    user: { ...ada, identity_kind: spaceKind === "team" ? "member" : "local_owner" },
  };
}

test("Invite says which of the two reasons leaves it with nobody to offer", () => {
  assert.equal(inviteUnavailableReason([ada, grace], [grace]), null);

  // Nobody else is enrolled: the fix is a team invitation, not this control.
  const alone = inviteUnavailableReason([ada], []);
  assert.match(alone, /only person in this space/i);
  assert.match(alone, /team invitation/i);

  // Everyone enrolled is already seated: nothing to fix.
  assert.match(inviteUnavailableReason([ada, grace], []), /already on this project/i);
});

test("a failed space read never claims you are alone in the space", () => {
  // The list request failed, so its own error is on screen. Inventing "you are
  // the only person here" from an empty list would state a fact nobody read.
  assert.equal(inviteUnavailableReason(null, []), null);
});

test("a personal project carries no invite control at all", () => {
  const html = renderToStaticMarkup(
    React.createElement(ProjectMembers, {
      projectId: "44444444-4444-4444-8444-444444444444",
      identity: identityIn("personal"),
      api: async () => [],
      onLeft() {},
    }),
  );

  assert.doesNotMatch(html, /Invite member/);
  assert.doesNotMatch(html, /<select/);
  assert.match(html, /Leave project/);
});

test("a team project keeps the invite control the backend supports", () => {
  const html = renderToStaticMarkup(
    React.createElement(ProjectMembers, {
      projectId: "44444444-4444-4444-8444-444444444444",
      identity: identityIn("team"),
      api: async () => [],
      onLeft() {},
    }),
  );

  assert.match(html, /Invite member/);
});
