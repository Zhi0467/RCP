import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { commentMentionCandidates, encodeCommentMentions } from "../mentions.ts";
import type { CommentMember } from "../types.ts";

const MAX_POST_CHARS = 2048;

/** A post field with the `@` picker over members; saving docks the thread at the
 *  surface's composer. */
export function CommentEditor({
  label,
  members,
  busy,
  initialText = "",
  autoFocus = false,
  secondary,
  onSubmit,
}: {
  label: "Comment" | "Reply";
  members: readonly CommentMember[];
  busy: boolean;
  initialText?: string;
  autoFocus?: boolean;
  /** Actions that lead the editor's own row, such as Cancel or Resolve. */
  secondary?: ReactNode;
  /** Resolves true once saved, which clears the field. */
  onSubmit: (text: string) => Promise<boolean>;
}) {
  const [text, setText] = useState(initialText);
  const [query, setQuery] = useState<{ at: number; text: string } | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (!autoFocus) return;
    const frame = requestAnimationFrame(() => input.current?.focus({ preventScroll: true }));
    return () => cancelAnimationFrame(frame);
  }, [autoFocus]);
  const candidates = useMemo(() => commentMentionCandidates(members), [members]);
  const empty = !text.trim();
  const submit = async (value: string) => {
    if (busy || !value.trim()) return;
    setQuery(null);
    if (await onSubmit(encodeCommentMentions(value.trim(), candidates))) setText("");
  };
  const readQuery = (field: HTMLTextAreaElement) => {
    const before = field.value.slice(0, field.selectionStart);
    const trigger = /(?:^|\s)@([^@\n]*)$/.exec(before);
    setQuery(trigger ? { at: before.lastIndexOf("@"), text: trigger[1].toLowerCase() } : null);
  };
  const pick = (label: string) => {
    const field = input.current;
    if (!field || !query) return;
    const end = field.selectionStart;
    const next = `${text.slice(0, query.at)}@${label} ${text.slice(end)}`;
    setQuery(null);
    setText(next);
    const caret = query.at + label.length + 2;
    requestAnimationFrame(() => {
      field.focus();
      field.setSelectionRange(caret, caret);
    });
  };
  const matches = query
    ? candidates.filter((candidate) => candidate.label.toLowerCase().includes(query.text))
    : [];
  return (
    <form
      className="rcp-comment-editor"
      onSubmit={(event) => {
        event.preventDefault();
        void submit(text);
      }}
    >
      <textarea
        ref={input}
        aria-label={label}
        placeholder={label}
        maxLength={MAX_POST_CHARS}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          readQuery(event.target);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && query) {
            event.stopPropagation();
            setQuery(null);
          }
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            void submit(text);
          }
        }}
      />
      {query && matches.length > 0 && (
        <div className="rcp-comment-picker" role="listbox" aria-label="Mention">
          {matches.map((candidate) => (
            <button
              key={candidate.token}
              type="button"
              role="option"
              aria-selected={false}
              onClick={() => pick(candidate.label)}
            >
              {candidate.label}
            </button>
          ))}
        </div>
      )}
      <div className="rcp-comment-actions">
        {secondary}
        <button type="submit" className="rcp-comment-primary" disabled={busy || empty}>
          {label}
        </button>
      </div>
    </form>
  );
}
