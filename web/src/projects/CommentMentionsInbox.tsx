import { useMemo } from "react";
import { useProjectResource } from "./projectResources";
import { commentMentionRows, commentMentionsProfile, relativeTime } from "./commentMentions";

/** The caller's open comment mentions; each row opens its thread. */
export function CommentMentionsInbox({ projectId }: { projectId: string }) {
  const profile = useMemo(() => commentMentionsProfile(projectId), [projectId]);
  const mentions = useProjectResource(profile).value;
  const rows = useMemo(() => commentMentionRows(projectId, mentions ?? []), [projectId, mentions]);
  if (rows.length === 0) return null;
  return (
    <section className="comment-mentions-inbox" aria-label="Mentions">
      <header className="rail-heading proposal-section-heading">
        <h2>Mentions</h2>
        <span className="count-badge">{rows.length}</span>
      </header>
      {rows.map((row) => (
        <a className="comment-mention-row" href={row.link} key={row.key}>
          <span className="comment-mention-avatar" aria-hidden>
            {row.author.trim().charAt(0).toUpperCase() || "?"}
          </span>
          <span className="comment-mention-main">
            <span className="comment-mention-topline">
              <strong>{row.author}</strong>
              <span className="comment-mention-target">on {row.targetName}</span>
              <time dateTime={row.createdAt} title={new Date(row.createdAt).toLocaleString()}>
                {relativeTime(row.createdAt)}
              </time>
            </span>
            <span className="comment-mention-excerpt">{row.excerpt}</span>
          </span>
        </a>
      ))}
    </section>
  );
}
