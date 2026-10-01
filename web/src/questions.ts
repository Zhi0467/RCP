import type { AgentQuestion } from "./types";

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

export function questionTranscript<T extends { timestamp: string }>(
  lines: T[],
  questions: AgentQuestion[],
) {
  return [
    ...lines.map((line) => ({ kind: "line" as const, line, timestamp: line.timestamp })),
    ...questions
      .filter((question) => !questionIsOpen(question) || question.withdrawn_readonly)
      .map((question) => ({ kind: "question" as const, question, timestamp: question.created_at })),
  ].sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
}
