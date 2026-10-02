import { useQuestions } from "../hooks/useQuestions";
import { QuestionCard } from "./QuestionCard";

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
  return (
    <div className="episode-questions">
      {error && <div role="alert">{error}</div>}
      {questions.map((question) => (
        <QuestionCard
          key={question.question_id}
          question={question}
          apiBase={apiBase}
          onResolved={refresh}
        />
      ))}
    </div>
  );
}
