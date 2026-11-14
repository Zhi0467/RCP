import { useEffect, useMemo, useRef, useState } from "react";
import { commentMentionCandidates, encodeCommentMentions } from "../mentions.ts";
import type { CommentMember } from "../types.ts";

const MAX_POST_CHARS = 2048;

/** A post field with the `@` picker: the target's agent first, then members.
 *  Picking the agent saves the post and sends its thread; an empty Reply sends the
 *  thread as it stands. */
export function CommentEditor({
  label,
  members,
  canSend,
  agentLabel,
  busy,
  autoFocus = false,
  onSubmit,
}: {
  label: "Comment" | "Reply";
  members: readonly CommentMember[];
  canSend: boolean;
  agentLabel: string;
  busy: boolean;
  autoFocus?: boolean;
  /** Resolves true once saved, which clears the field. */
  onSubmit: (text: string, send: boolean) => Promise<boolean>;
}) {
  const [text, setText] = useState("");
  const [query, setQuery] = useState<{ at: number; text: string } | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (!autoFocus) return;
    const frame = requestAnimationFrame(() => input.current?.focus({ preventScroll: true }));
    return () => cancelAnimationFrame(frame);
  }, [autoFocus]);
  const candidates = useMemo(() => commentMentionCandidates(members, false), [members]);
  const empty = !text.trim();
  const submit = async (value: string, send: boolean) => {
    if (busy || (!value.trim() && !(send && label === "Reply"))) return;
    setQuery(null);
    if (await onSubmit(encodeCommentMentions(value.trim(), candidates), send)) setText("");
  };
  const readQuery = (field: HTMLTextAreaElement) => {
    const before = field.value.slice(0, field.selectionStart);
    const trigger = /(?:^|\s)@([^@\n]*)$/.exec(before);
    setQuery(trigger ? { at: before.lastIndexOf("@"), text: trigger[1].toLowerCase() } : null);
  };
  const pick = (label: string | null) => {
    const field = input.current;
    if (!field || !query) return;
    const end = field.selectionStart;
    const next = `${text.slice(0, query.at)}${label === null ? "" : `@${label} `}${text.slice(end)}`;
    setQuery(null);
    if (label === null) {
      void submit(next, true);
      return;
    }
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
        void submit(text, false);
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
            void submit(text, false);
          }
        }}
      />
      {query && (canSend || matches.length > 0) && (
        <div className="rcp-comment-picker" role="listbox" aria-label="Mention">
          {canSend && (
            <button type="button" role="option" aria-selected={false} onClick={() => pick(null)}>
              {agentLabel}
            </button>
          )}
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
        <button type="submit" disabled={busy || empty}>
          {label}
        </button>
        {canSend && (
          <button
            type="button"
            className="rcp-comment-primary"
            disabled={busy || (empty && label !== "Reply")}
            onClick={() => void submit(text, true)}
          >
            Send to agent
          </button>
        )}
      </div>
    </form>
  );
}
