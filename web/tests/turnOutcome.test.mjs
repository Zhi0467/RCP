import assert from "node:assert/strict";
import test from "node:test";
import { turnOutcomeChip } from "../src/chat/turnOutcome.ts";

test("a Done turn has no chip; the others are distinct and keep reasons for incomplete turns", () => {
  assert.equal(turnOutcomeChip({ status: "done", reason: null }), null);
  assert.equal(turnOutcomeChip(null), null);
  const statuses = ["partial", "could_not", "not_stated"];
  const chips = statuses.map((status) => turnOutcomeChip({ status, reason: "source unavailable" }));
  assert.deepEqual(
    chips.map((chip) => chip.status),
    statuses,
  );
  assert.equal(new Set(chips.map((chip) => chip.label)).size, statuses.length);
  assert.deepEqual(
    chips.map((chip) => Boolean(chip.reason)),
    [true, true, false],
  );
});
