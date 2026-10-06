import { useId, useState } from "react";
import { answerQuestion, dismissQuestion } from "../core/api";
import { canSubmitQuestion, questionIsOpen, toggleQuestionChoice } from "./questions";
import type { AgentQuestion } from "../core/types";

export function QuestionCard({
  question,
  apiBase,
  onResolved,
  continueChat = false,
}: {
  question: AgentQuestion;
  apiBase: string;
  onResolved: () => void;
  continueChat?: boolean;
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
      <header className="question-header">
        <span className="question-state">
          {question.withdrawn_readonly
            ? "Episode ended"
            : open
              ? "Needs you"
              : question.state === "answered"
                ? "Answered"
                : "Dismissed"}
        </span>
        {open && (
          <button
            className="question-dismiss"
            type="button"
            disabled={busy}
            onClick={() => void resolve("dismiss")}
          >
            Dismiss
          </button>
        )}
      </header>
      <strong id={labelId} className="question-text">
        {question.question}
      </strong>
      {open ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void resolve("answer");
          }}
        >
          {question.choices.length > 0 && (
            <div className="question-choices">
              {question.choices.map((choice, index) => (
                <button
                  key={choice}
                  className="question-choice"
                  type="button"
                  disabled={disabled}
                  aria-pressed={question.multiple ? choices.includes(choice) : undefined}
                  onClick={() =>
                    question.multiple
                      ? setChoices(toggleQuestionChoice(choices, choice))
                      : void resolve("answer", [choice])
                  }
                >
                  <span className="question-choice-key" aria-hidden="true">
                    {choiceKey(index)}
                  </span>
                  <span>{choice}</span>
                </button>
              ))}
            </div>
          )}
          <div className="question-actions">
            <textarea
              aria-label="Question answer"
              rows={1}
              placeholder={question.choices.length ? "Or type your own answer" : "Your answer"}
              value={text}
              disabled={disabled}
              onChange={(event) => setText(event.target.value)}
            />
            <button
              className="button primary compact"
              type="submit"
              disabled={busy || !canSubmitQuestion(question, text, choices)}
            >
              {continueChat
                ? `Answer and continue ${question.capability === "discuss" ? "Discuss" : "Work"}`
                : "Answer"}
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

/** A, B, … Z, then 27, 28, …: a key the reader can name aloud. */
function choiceKey(index: number): string {
  return index < 26 ? String.fromCharCode(65 + index) : String(index + 1);
}
