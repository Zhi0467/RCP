import { api } from "../core/api";
import { buildNotificationLink } from "../core/notificationLinks";
import type { ResourceProfile } from "../core/resourceStore";
import { bulletinDecodeMentions, bulletinMentionCandidates } from "../chat/bulletinModel";
import { anchorQuote } from "../comments/model.ts";
import type { CommentMention } from "../comments/types.ts";
import { projectResourceKey } from "./projectResources";

export interface CommentMentionRow {
  key: string;
  author: string;
  targetName: string;
  excerpt: string;
  createdAt: string;
  link: string;
}

/** Any post or thread change moves the `comments` watermark, which re-reads the list. */
export function commentMentionsProfile(projectId: string): ResourceProfile<CommentMention[]> {
  const path = `/api/projects/${encodeURIComponent(projectId)}/comment-mentions`;
  return {
    key: projectResourceKey(projectId, "comments", { query: path }),
    fetch: (signal) => api(path, { signal }),
  };
}

/** The mentioning post's author, and the anchor's quote or else the thread's first post. */
export function commentMentionRows(
  projectId: string,
  mentions: readonly CommentMention[],
): CommentMentionRow[] {
  return mentions.flatMap((mention) => {
    const { posts, anchor, graph_target } = mention.thread;
    const post = posts.find((item) => item.post_id === mention.post_id);
    if (!post) return [];
    const members = new Map(
      [...(mention.members ?? []), ...posts.map((item) => item.author)].map((member) => [
        member.user_id,
        member,
      ]),
    );
    const known = bulletinMentionCandidates([...members.values()], []);
    return [
      {
        key: `${mention.post_id}:${mention.thread_id}`,
        author: post.author.display_name || post.author.user_id,
        targetName: mention.target_name,
        excerpt: anchorQuote(anchor) || bulletinDecodeMentions(posts[0].text, known),
        createdAt: mention.created_at,
        link: buildNotificationLink({
          projectId,
          target: graph_target,
          kind: "comment",
          itemId: mention.thread_id,
        }),
      },
    ];
  });
}

const RELATIVE_UNITS: Array<[Intl.RelativeTimeFormatUnit, number]> = [
  ["year", 365 * 24 * 3600],
  ["month", 30 * 24 * 3600],
  ["week", 7 * 24 * 3600],
  ["day", 24 * 3600],
  ["hour", 3600],
  ["minute", 60],
];

export function relativeTime(iso: string, now: Date = new Date()): string {
  const seconds = (new Date(iso).getTime() - now.getTime()) / 1000;
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" });
  for (const [unit, size] of RELATIVE_UNITS) {
    if (Math.abs(seconds) >= size) return format.format(Math.round(seconds / size), unit);
  }
  return format.format(0, "minute");
}
