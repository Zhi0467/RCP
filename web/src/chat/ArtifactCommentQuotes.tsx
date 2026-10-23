import { File, SquareDashed } from "lucide-react";
import type { ChatArtifactComments } from "../core/types";

/** The free text before the numbered comments, or null when the message is not their composition. */
export function artifactCommentLead(text: string, comments: ChatArtifactComments): string | null {
  // Admission composes the turn as any free text, then "Comment N: …" per selection.
  const numbered = comments.quotes
    .map((quote, index) => `Comment ${index + 1}: ${quote.comment.trim()}`)
    .join("\n\n");
  return text.endsWith(numbered) ? text.slice(0, text.length - numbered.length).trim() : null;
}

/** A human turn's artifact comments, each under what it was drawn on. */
export function ArtifactCommentQuotes({
  lead,
  comments,
  onOpen,
}: {
  lead: string;
  comments: ChatArtifactComments;
  onOpen: () => void;
}) {
  return (
    <>
      {comments.name && (
        <button type="button" className="chat-input-reference" onClick={onOpen}>
          <File size={14} aria-hidden="true" />
          <strong>{comments.name}</strong>
        </button>
      )}
      {lead && <span className="node-chat-text">{lead}</span>}
      <ol className="chat-artifact-comments">
        {comments.quotes.map((quote, index) => (
          <li key={index}>
            <blockquote>
              {quote.kind === "box" && <SquareDashed size={12} aria-hidden="true" />}
              {quote.kind === "box" ? quote.quote || "Selected area" : quote.quote}
            </blockquote>
            <span className="node-chat-text">{quote.comment.trim()}</span>
          </li>
        ))}
      </ol>
    </>
  );
}
