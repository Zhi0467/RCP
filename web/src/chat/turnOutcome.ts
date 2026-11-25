import type { TurnOutcome, TurnOutcomeStatus } from "../core/types";

const labels: Record<Exclude<TurnOutcomeStatus, "done">, string> = {
  partial: "Partial",
  could_not: "Couldn't do it",
  not_stated: "No outcome stated",
};

/** A finished turn's chip; a Done turn shows none. */
export function turnOutcomeChip(outcome: TurnOutcome | null | undefined) {
  if (!outcome || outcome.status === "done") return null;
  return {
    status: outcome.status,
    label: labels[outcome.status],
    reason: outcome.status === "partial" || outcome.status === "could_not" ? outcome.reason : null,
  };
}
