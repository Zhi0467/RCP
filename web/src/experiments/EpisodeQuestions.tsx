import { useQuestions } from "../chat/useQuestions";
import { QuestionCard } from "../chat/QuestionCard";
import { questionIsOpen } from "../chat/questions";

export function EpisodeQuestions({
  apiBase,
  episodeId,
  freshness,
}: {
  apiBase: string;
  episodeId: string;
  freshness: string;
}) {
  const { questions, error, refresh } = useQuestions(apiBase, "episode", episodeId, freshness);
  const openCount = questions.filter(
    (question) => questionIsOpen(question) && !question.withdrawn_readonly,
  ).length;
  return (
    <div className="episode-questions">
      {error && <div role="alert">{error}</div>}
      {questions.length > 0 && (
        // Open questions keep the fold open; resolved ones fold away.
        <details className="experiment-fold nested" open={openCount > 0}>
          <summary>
            <span className="experiment-fold-title">Questions</span>
            <span className="experiment-fold-count">
              {openCount > 0 ? `${openCount}/${questions.length}` : questions.length}
            </span>
          </summary>
          <div className="experiment-fold-body">
            {questions.map((question) => (
              <QuestionCard
                key={question.question_id}
                question={question}
                apiBase={apiBase}
                onResolved={refresh}
              />
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
