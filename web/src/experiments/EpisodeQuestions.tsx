import { useQuestions } from "../chat/useQuestions";
import { QuestionCard } from "../chat/QuestionCard";

export function EpisodeQuestions({ apiBase, episodeId }: { apiBase: string; episodeId: string }) {
  const { questions, error, refresh } = useQuestions(apiBase, "episode", episodeId);
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
