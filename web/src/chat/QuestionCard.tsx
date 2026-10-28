import { useId, useRef, useState } from "react";
import { answerQuestion, dismissQuestion } from "../core/api";
import { MarkdownAnswer } from "../core/chatMarkdown";
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
  const [turns, setTurns] = useState("3");
  // A retry reuses its request id while the turns asked are unchanged.
  const continuation = useRef<{ turns: number; id: string } | null>(null);
  const open = questionIsOpen(question) && !question.withdrawn_readonly;
  // The ended episode's last open card carries the turns its answer adds.
  const addsTurns = question.answer_continues === "turns";
  const parsedTurns = Number(turns);
  const turnsValid = Number.isSafeInteger(parsedTurns) && parsedTurns >= 1;
  const answerBlocked = question.answer_continues === "ending" || (addsTurns && !turnsValid);
  const disabled = busy || !question.can_answer;
  async function resolve(action: "answer" | "dismiss", selection = choices) {
    if (
      busy ||
      (action === "answer" && (answerBlocked || !canSubmitQuestion(question, text, selection)))
    )
      return;
    setBusy(true);
    setError(null);
    try {
      if (action === "answer") {
        if (addsTurns && continuation.current?.turns !== parsedTurns)
          continuation.current = { turns: parsedTurns, id: crypto.randomUUID() };
        await answerQuestion(apiBase, question.question_id, {
          answer: text,
          choices: selection,
          ...(addsTurns && continuation.current
            ? {
                invocation_ceiling: continuation.current.turns,
                request_id: continuation.current.id,
              }
            : {}),
        });
      } else await dismissQuestion(apiBase, question.question_id);
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
      data-agent-waiting={open && question.agent_waiting}
      data-question-state={question.withdrawn_readonly ? "ended" : question.state}
    >
      <header className="question-header">
        <span className="question-state">
          {question.withdrawn_readonly
            ? "Episode ended"
            : open
              ? question.agent_waiting
                ? "Agent is waiting"
                : "Needs you"
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
      <div id={labelId} className="question-text chat-markdown">
        <MarkdownAnswer text={question.question} />
      </div>
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
                  disabled={disabled || (!question.multiple && answerBlocked)}
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
            {addsTurns && (
              <label className="question-turns">
                Add turns
                <input
                  type="number"
                  min={1}
                  step={1}
                  aria-label="Turns this answer adds"
                  value={turns}
                  disabled={disabled}
                  onChange={(event) => setTurns(event.target.value)}
                />
              </label>
            )}
            <button
              className="button primary compact"
              type="submit"
              disabled={busy || answerBlocked || !canSubmitQuestion(question, text, choices)}
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
