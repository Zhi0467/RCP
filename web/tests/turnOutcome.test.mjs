import assert from "node:assert/strict";
import test from "node:test";
import { turnOutcomeChip } from "../src/chat/turnOutcome.ts";

test("outcome chips distinguish all statuses and retain reasons for incomplete turns", () => {
  const statuses = ["done", "partial", "could_not", "not_stated"];
  const chips = statuses.map((status) => turnOutcomeChip({ status, reason: "source unavailable" }));
  assert.deepEqual(
    chips.map((chip) => chip.status),
    statuses,
  );
  assert.equal(new Set(chips.map((chip) => chip.label)).size, statuses.length);
  assert.deepEqual(
    chips.map((chip) => Boolean(chip.reason)),
    [false, true, true, false],
  );
});
