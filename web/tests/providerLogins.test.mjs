import assert from "node:assert/strict";
import test from "node:test";

import { accountLabel, providerLabel, signInNote, tokenNote } from "../src/providerLogins.ts";

test("the account label names the machine account and the project aliases that use it", () => {
  assert.equal(accountLabel({ host: "", machines: [] }, "team"), "Team server");
  assert.equal(
    accountLabel({ host: "", machines: ["local"] }, "personal"),
    "Local machine (local)",
  );
  assert.equal(
    accountLabel({ host: "gpu.example", machines: ["gpu", "gpu.example"] }, "team"),
    "gpu.example (gpu)",
  );
  assert.equal(providerLabel("codex"), "Codex");
  assert.equal(providerLabel("claude"), "Claude");
});

test("the token note is an estimate that never contains the token", () => {
  const now = new Date("2026-09-14T12:00:00Z");
  const token = {
    pasted_at: "2026-09-14T10:00:00Z",
    pasted_by: "member",
    verified_at: "2026-09-14T10:00:05Z",
    estimated_expiry_at: "2027-08-15T10:00:00Z",
  };
  const note = tokenNote(token, now);
  assert.match(note, /saved by member, verified/);
  assert.match(note, /about 334 more days/);
  assert.match(tokenNote({ ...token, verified_at: null }, now), /not verified yet/);
  assert.match(
    tokenNote({ ...token, estimated_expiry_at: "2026-09-01T00:00:00Z" }, now),
    /estimated lifetime ended/,
  );
});

test("the sign-in note follows the device-code flow", () => {
  const base = {
    login_id: "login",
    provider: "codex",
    host: "",
    state: "pending",
    user_code: null,
    verification_url: null,
    detail: null,
    started_at: "2026-09-14T10:00:00Z",
    started_by: "member",
    finished_at: null,
    resumed: null,
  };
  assert.match(signInNote(base), /waiting for its device code/);
  assert.match(
    signInNote({ ...base, user_code: "ABCD-EFGH", verification_url: "https://auth.example/d" }),
    /enter the code/,
  );
  assert.match(signInNote({ ...base, state: "succeeded" }), /verified/);
  assert.equal(
    signInNote({ ...base, state: "failed", detail: "denied" }),
    "Sign-in failed: denied",
  );
});
