import assert from "node:assert/strict";
import test from "node:test";
import {
  projectGroupShelf,
  projectGroupHash,
  parseProjectGroupHash,
  canDropProject,
} from "../src/projects/projectGroups.ts";
import {
  projectCreateSetupHash,
  projectProvisioningHash,
  parseProjectSetupRoute,
  buildTeamProvisioningRequest,
} from "../src/projects/projectSetupModel.ts";

const groups = [{ id: "g1" }, { id: "g2" }];
const projects = [
  { id: "p1", group_id: null, attention_count: 1 },
  { id: "p2", group_id: "g1", attention_count: 2 },
  { id: "p3", group_id: "g1", attention_count: 3 },
];
test("shelf partitions projects and retains empty visible groups", () => {
  const shelf = projectGroupShelf(projects, groups);
  assert.deepEqual(
    shelf.ungrouped.map((p) => p.id),
    ["p1"],
  );
  assert.deepEqual(
    shelf.groups.map((g) => [g.id, g.projects.map((p) => p.id), g.count, g.waiting]),
    [
      ["g1", ["p2", "p3"], 2, 5],
      ["g2", [], 0, 0],
    ],
  );
});
test("group hashes round trip without claiming project or setup routes", () => {
  for (const id of ["g1", "with / ? #", "粒子"])
    assert.equal(parseProjectGroupHash(projectGroupHash(id)), id);
  for (const hash of [
    projectGroupHash(null),
    "#/projects/p1",
    "#/projects/new",
    "#/project-groups/%",
    "#/project-groups/",
  ])
    assert.equal(parseProjectGroupHash(hash), null);
});
test("drop targets require a project and a different visible destination", () => {
  assert.equal(canDropProject(projects[0], "g1", groups), true);
  assert.equal(canDropProject(projects[1], null, groups), true);
  assert.equal(canDropProject(projects[1], "g2", groups), true);
  for (const [project, target] of [
    [undefined, "g1"],
    [projects[0], null],
    [projects[1], "g1"],
    [projects[0], "missing"],
  ])
    assert.equal(canDropProject(project, target, groups), false);
});
test("setup and provisioning preserve selected group", () => {
  const groupId = "11111111-1111-4111-8111-111111111111";
  const requestId = "22222222-2222-4222-8222-222222222222";
  assert.equal(parseProjectSetupRoute(projectCreateSetupHash(groupId)).groupId, groupId);
  assert.deepEqual(parseProjectSetupRoute(projectProvisioningHash(requestId, groupId)), {
    kind: "create",
    requestId,
    groupId,
  });
  const request = buildTeamProvisioningRequest({
    name: "x",
    groupId,
    stateRepository: "r",
    defaultAutoResearchInvocationCeiling: 1,
    machines: [],
    repositories: [],
    providerChecks: [],
  });
  assert.equal(request.group_id, groupId);
});
