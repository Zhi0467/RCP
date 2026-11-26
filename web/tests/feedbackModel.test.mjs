import assert from "node:assert/strict";
import test from "node:test";

import { feedbackIssueUrl } from "../src/feedback/feedbackModel.ts";

test("feedback becomes a GitHub issue titled by its first line and closed by the version", () => {
  const url = new URL(
    feedbackIssueUrl("  Reports need a dark figure palette\nThe bars vanish in dark mode.  ", {
      version: "0.4.54",
      build: 3480,
      space_kind: "team",
    }),
  );
  assert.equal(url.origin + url.pathname, "https://github.com/Zhi0467/RCP/issues/new");
  assert.equal(url.searchParams.get("title"), "Reports need a dark figure palette");
  const body = url.searchParams.get("body");
  assert.ok(body.startsWith("Reports need a dark figure palette\nThe bars vanish in dark mode."));
  assert.ok(body.endsWith("0.4.54 build 3480, team space"));
});

test("a long first line is shortened and an unknown version leaves the body alone", () => {
  const line = "x".repeat(200);
  const url = new URL(feedbackIssueUrl(line, null));
  assert.equal(url.searchParams.get("title").length, 72);
  assert.equal(url.searchParams.get("body"), line);
});
