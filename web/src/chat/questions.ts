import type { AgentQuestion } from "../types";

export function questionIsOpen(question: AgentQuestion): boolean {
  return question.state === "pending" || question.state === "parked";
}

export function canSubmitQuestion(
  question: AgentQuestion,
  text: string,
  choices: string[],
): boolean {
  return (
    questionIsOpen(question) &&
    question.can_answer &&
    !question.withdrawn_readonly &&
    (!!text.trim() || choices.length > 0) &&
    choices.every((choice) => question.choices.includes(choice)) &&
    new Set(choices).size === choices.length &&
    (question.multiple || choices.length <= 1)
  );
}

export function toggleQuestionChoice(selected: string[], choice: string): string[] {
  return selected.includes(choice)
    ? selected.filter((item) => item !== choice)
    : [...selected, choice];
}

/** Keeps transcript order and slots each resolved question before the first later line. */
export function questionTranscript<T extends { timestamp: string }>(
  lines: T[],
  questions: AgentQuestion[],
) {
  type Entry = { kind: "line"; line: T } | { kind: "question"; question: AgentQuestion };
  const pending = questions
    .filter((question) => !questionIsOpen(question) || question.withdrawn_readonly)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const entries: Entry[] = [];
  for (const line of lines) {
    const at = Date.parse(line.timestamp);
    while (pending.length > 0 && !Number.isNaN(at) && Date.parse(pending[0].created_at) < at) {
      entries.push({ kind: "question", question: pending.shift()! });
    }
    entries.push({ kind: "line", line });
  }
  return [...entries, ...pending.map((question) => ({ kind: "question" as const, question }))];
}
