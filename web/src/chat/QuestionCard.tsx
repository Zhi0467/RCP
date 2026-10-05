import { useId, useState } from "react";
import { answerQuestion, dismissQuestion } from "../api";
import { canSubmitQuestion, questionIsOpen, toggleQuestionChoice } from "./questions";
import type { AgentQuestion } from "../types";

export function QuestionCard({
  question,
  apiBase,
  onResolved,
  continueWork = false,
}: {
  question: AgentQuestion;
  apiBase: string;
  onResolved: () => void;
  continueWork?: boolean;
}) {
  const labelId = useId();
  const [text, setText] = useState("");
  const [choices, setChoices] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = questionIsOpen(question) && !question.withdrawn_readonly;
  const disabled = busy || !question.can_answer;
  async function resolve(action: "answer" | "dismiss", selection = choices) {
    if (busy || (action === "answer" && !canSubmitQuestion(question, text, selection))) return;
    setBusy(true);
    setError(null);
    try {
      if (action === "answer")
        await answerQuestion(apiBase, question.question_id, { answer: text, choices: selection });
      else await dismissQuestion(apiBase, question.question_id);
      onResolved();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className="question-card"
      aria-labelledby={labelId}
      data-question-id={question.question_id}
      data-question-state={question.withdrawn_readonly ? "ended" : question.state}
    >
      <strong id={labelId}>{question.question}</strong>
      <span className="question-state">
        {question.withdrawn_readonly
          ? "Episode ended"
          : open
            ? "Needs you"
            : question.state === "answered"
              ? "Answered"
              : "Dismissed"}
      </span>
      {open ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void resolve("answer");
          }}
        >
          {question.choices.length > 0 && (
            <div className="question-choices">
              {question.choices.map((choice) => (
                <button
                  key={choice}
                  className="button compact"
                  type="button"
                  disabled={disabled}
                  aria-pressed={question.multiple ? choices.includes(choice) : undefined}
                  onClick={() =>
                    question.multiple
                      ? setChoices(toggleQuestionChoice(choices, choice))
                      : void resolve("answer", [choice])
                  }
                >
                  {choice}
                </button>
              ))}
            </div>
          )}
          <textarea
            aria-label="Question answer"
            rows={2}
            value={text}
            disabled={disabled}
            onChange={(event) => setText(event.target.value)}
          />
          <div className="question-actions">
            <button
              className="button primary compact"
              type="submit"
              disabled={busy || !canSubmitQuestion(question, text, choices)}
            >
              {continueWork ? "Answer and continue Work" : "Answer"}
            </button>
            <button
              className="button compact"
              type="button"
              disabled={busy}
              onClick={() => void resolve("dismiss")}
            >
              Dismiss
            </button>
          </div>
        </form>
      ) : (
        <>
          {question.chosen_choices.length > 0 && (
            <ul>
              {question.chosen_choices.map((choice) => (
                <li key={choice}>{choice}</li>
              ))}
            </ul>
          )}
          {question.answer && <p className="question-answer">{question.answer}</p>}
          {question.resolved_by && (
            <span>
              {question.resolved_by.display_name}
              {question.resolved_at && (
                <>
                  {" "}
                  ·{" "}
                  <time dateTime={question.resolved_at}>
                    {new Date(question.resolved_at).toLocaleString()}
                  </time>
                </>
              )}
            </span>
          )}
        </>
      )}
      {error && <div role="alert">{error}</div>}
    </section>
  );
}
