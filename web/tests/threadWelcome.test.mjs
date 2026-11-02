import assert from "node:assert/strict";
import test from "node:test";

import { threadWelcome } from "../src/agents/threadWelcome.ts";

const space = {
  kind: "space",
  agent_name: "Pip",
  project_name: null,
  capabilities: ["a", "b", "c"],
};
const project = {
  kind: "project",
  agent_name: "Puck",
  project_name: "Warmup study",
  capabilities: ["d", "e"],
};

test("each thread kind welcomes with its own capabilities and starters", () => {
  const pip = threadWelcome(space, "Warmup study");
  const puck = threadWelcome(project, "Warmup study");
  // The lines are the backend's capabilities for the thread, verbatim.
  assert.deepEqual(pip.lines, space.capabilities);
  assert.deepEqual(puck.lines, project.capabilities);
  assert.equal(pip.prompts.length, 3);
  assert.equal(puck.prompts.length, 3);
  assert.notDeepEqual(pip.prompts, puck.prompts);
});

test("Pip's last starter names the first project and stands alone without one", () => {
  const named = threadWelcome(space, "Warmup study").prompts.at(-1);
  const none = threadWelcome(space, null).prompts.at(-1);
  assert.ok(named.includes("Warmup study"));
  assert.ok(none && !none.includes("Warmup study"));
  assert.notEqual(named, none);
});

test("no capabilities show no lines", () => {
  assert.equal(threadWelcome({ ...project, capabilities: [] }, null).lines.length, 0);
});
