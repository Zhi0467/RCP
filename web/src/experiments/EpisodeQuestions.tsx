import { useQuestions } from "../chat/useQuestions";
import { QuestionCard } from "../chat/QuestionCard";
import { questionIsOpen } from "../chat/questions";

export function EpisodeQuestions({ apiBase, episodeId }: { apiBase: string; episodeId: string }) {
  const { questions, error, refresh } = useQuestions(apiBase, "episode", episodeId);
  const newestFirst = [...questions].sort((a, b) => b.created_at.localeCompare(a.created_at));
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
            {openCount > 0 ? (
              <span className="episode-questions-open">
                {openCount} {openCount === 1 ? "needs" : "need"} you
              </span>
            ) : (
              <span className="experiment-fold-count">{questions.length}</span>
            )}
          </summary>
          <div className="experiment-fold-body">
            {newestFirst.map((question) => (
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
