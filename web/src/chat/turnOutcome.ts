import type { TurnOutcome, TurnOutcomeStatus } from "../core/types";

const labels: Record<TurnOutcomeStatus, string> = {
  done: "Done",
  partial: "Partial",
  could_not: "Couldn't do it",
  not_stated: "No outcome stated",
};

export function turnOutcomeChip(outcome: TurnOutcome) {
  return {
    status: outcome.status,
    label: labels[outcome.status],
    reason: outcome.status === "partial" || outcome.status === "could_not" ? outcome.reason : null,
  };
}
