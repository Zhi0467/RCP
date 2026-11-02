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

test("each thread kind welcomes with its own agent, capabilities and starters", () => {
  const pip = threadWelcome(space, "Warmup study");
  const puck = threadWelcome(project, "Warmup study");
  assert.equal(pip.title, "Pip");
  assert.equal(puck.title, "Puck");
  assert.equal(puck.subtitle, "Warmup study");
  // The lines are the backend's capabilities for the thread, verbatim.
  assert.deepEqual(pip.lines, space.capabilities);
  assert.deepEqual(puck.lines, project.capabilities);
  assert.equal(pip.prompts.length, 3);
  assert.equal(puck.prompts.length, 3);
  assert.notDeepEqual(pip.prompts, puck.prompts);
  assert.notEqual(pip.subtitle, puck.subtitle);
});

test("Pip's last starter names the first project and stands alone without one", () => {
  const named = threadWelcome(space, "Warmup study").prompts.at(-1);
  const none = threadWelcome(space, null).prompts.at(-1);
  assert.ok(named.includes("Warmup study"));
  assert.ok(none && !none.includes("Warmup study"));
  assert.notEqual(named, none);
});

test("a nameless thread falls back to a title, and no capabilities show no lines", () => {
  const nameless = threadWelcome({ ...project, agent_name: "", capabilities: [] }, null);
  assert.equal(nameless.title, "Warmup study");
  assert.equal(nameless.lines.length, 0);
  assert.ok(threadWelcome({ ...space, agent_name: "" }, null).title);
});
