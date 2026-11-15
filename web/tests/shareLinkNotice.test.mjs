import assert from "node:assert/strict";
import { test } from "node:test";

import { sshTargetForHost } from "../src/desktop/shareLinkNoticeModel.ts";

test("the prefilled SSH target drops the port and IPv6 brackets", () => {
  assert.equal(sshTargetForHost("rcp.lab.example"), "rcp.lab.example");
  assert.equal(sshTargetForHost("rcp.lab.example:8443"), "rcp.lab.example");
  assert.equal(sshTargetForHost("[fe80::1]:8443"), "fe80::1");
  assert.equal(sshTargetForHost("[fe80::1]"), "fe80::1");
});
