import { NotebookPen, UserRound } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  addLesson,
  deleteLesson,
  editLesson,
  LESSON_TEXT_MAX_CHARS,
  lessonTextIsValid,
  loadLessons,
} from "../consolidation";
import type { Lesson } from "../types";

interface Props {
  apiBase: string;
  writesDisabled: boolean;
}

/** The project's operational lessons: members list, add, edit, and delete them. */
export function ProjectLessons({ apiBase, writesDisabled }: Props) {
  // Keyed by project so a response for the previous project never shows here.
  const [loaded, setLoaded] = useState<{ apiBase: string; lessons: Lesson[] } | null>(null);
  const lessons = loaded?.apiBase === apiBase ? loaded.lessons : null;
  const currentApiBase = useRef(apiBase);
  currentApiBase.current = apiBase;
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<{ lessonId: string; text: string } | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    const { lessons } = await loadLessons(apiBase);
    if (currentApiBase.current === apiBase) setLoaded({ apiBase, lessons });
  }, [apiBase]);

  useEffect(() => {
    void reload().catch((failure) =>
      setError(failure instanceof Error ? failure.message : String(failure)),
    );
  }, [reload]);

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      await reload();
      return true;
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
      await reload().catch(() => {});
      return false;
    } finally {
      setBusy(false);
    }
  };
  const disabled = busy || writesDisabled || lessons === null;

  return (
    <section className="settings-section project-lessons">
      <header>
        <span>
          <NotebookPen size={16} />
        </span>
        <h2>Lessons</h2>
      </header>
      <ul className="project-lesson-list">
        {(lessons ?? []).map((lesson) => (
          <li key={lesson.lesson_id}>
            {editing?.lessonId === lesson.lesson_id ? (
              <textarea
                aria-label="Lesson text"
                value={editing.text}
                maxLength={LESSON_TEXT_MAX_CHARS}
                disabled={busy}
                onChange={(event) => setEditing({ ...editing, text: event.target.value })}
              />
            ) : (
              <p>{lesson.text}</p>
            )}
            <span className="project-lesson-meta">
              {lesson.human_owned ? <UserRound size={12} aria-label="Human-owned" /> : null}
              {lesson.author.kind === "human"
                ? lesson.author.display_name || lesson.author.user_id
                : "Agent"}
              {" · "}
              {new Date(lesson.updated_at).toLocaleDateString()}
            </span>
            <div className="project-lesson-actions">
              {editing?.lessonId === lesson.lesson_id ? (
                <>
                  <button
                    className="button secondary compact"
                    type="button"
                    disabled={disabled || !lessonTextIsValid(editing.text)}
                    onClick={async () => {
                      if (
                        await run(() => editLesson(apiBase, lesson.lesson_id, editing.text.trim()))
                      )
                        setEditing(null);
                    }}
                  >
                    Save
                  </button>
                  <button
                    className="button secondary compact"
                    type="button"
                    disabled={busy}
                    onClick={() => setEditing(null)}
                  >
                    Cancel
                  </button>
                </>
              ) : confirmingDelete === lesson.lesson_id ? (
                <>
                  <button
                    className="button secondary compact danger"
                    type="button"
                    disabled={disabled}
                    onClick={async () => {
                      if (await run(() => deleteLesson(apiBase, lesson.lesson_id)))
                        setConfirmingDelete(null);
                    }}
                  >
                    Confirm delete
                  </button>
                  <button
                    className="button secondary compact"
                    type="button"
                    disabled={busy}
                    onClick={() => setConfirmingDelete(null)}
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <>
                  <button
                    className="button secondary compact"
                    type="button"
                    disabled={disabled}
                    onClick={() => {
                      setConfirmingDelete(null);
                      setEditing({ lessonId: lesson.lesson_id, text: lesson.text });
                    }}
                  >
                    Edit
                  </button>
                  <button
                    className="button secondary compact"
                    type="button"
                    disabled={disabled}
                    onClick={() => {
                      setEditing(null);
                      setConfirmingDelete(lesson.lesson_id);
                    }}
                  >
                    Delete
                  </button>
                </>
              )}
            </div>
          </li>
        ))}
      </ul>
      <div className="project-member-actions">
        <textarea
          aria-label="New lesson"
          value={draft}
          maxLength={LESSON_TEXT_MAX_CHARS}
          disabled={disabled}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          className="button secondary compact"
          type="button"
          disabled={disabled || !lessonTextIsValid(draft)}
          onClick={async () => {
            if (await run(() => addLesson(apiBase, draft.trim()))) setDraft("");
          }}
        >
          Add
        </button>
      </div>
      {error ? <p className="project-member-error">{error}</p> : null}
    </section>
  );
}
