import assert from "node:assert/strict";
import test from "node:test";
import { artifactPopupTarget } from "../src/artifacts/artifactViewerRequests.ts";

test("only recognised viewer entrances in the active space become panel targets", () => {
  const origin = "https://rcp.example";
  for (const action of ["viewer", "preview", "content", "download"]) {
    assert.deepEqual(
      artifactPopupTarget(
        `${origin}/api/projects/project%20one/artifacts/a/${action}?v=2#part`,
        origin,
        "project one",
      ),
      {
        kind: "artifact",
        projectId: "project one",
        artifactId: "a",
      },
    );
    assert.deepEqual(
      artifactPopupTarget(`${origin}/api/projects/p/episodes/e/report/${action}`, origin, "p"),
      {
        kind: "report",
        projectId: "p",
        episodeId: "e",
      },
    );
  }
  for (const url of [
    "https://other.example/api/projects/p/artifacts/a/viewer",
    `${origin}/api/projects/p/artifacts/a/state`,
    `${origin}/api/projects/p/artifacts/a/comments`,
    `${origin}/api/projects/p/episodes/e/report/viewer/extra`,
    `${origin}/api/projects/%zz/artifacts/a/viewer`,
    `${origin}/settings`,
    "about:blank",
    "not a URL",
  ])
    assert.equal(artifactPopupTarget(url, origin, "p"), null);
});

test("popup targets must belong to the currently open project", () => {
  for (const route of ["artifacts/a/viewer", "episodes/e/report/viewer"])
    for (const projectId of ["other", null])
      assert.equal(
        artifactPopupTarget(
          `https://rcp.example/api/projects/p/${route}`,
          "https://rcp.example",
          projectId,
        ),
        null,
      );
});
