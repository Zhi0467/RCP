export interface BulletinPost {
  post_id: string;
  author: {
    kind: "agent" | "member";
    operation_id?: string;
    actor_label?: string;
    user_id?: string;
    display_name?: string;
  };
  text: string;
  refs: string[];
  reply_to: string | null;
  created_at: string;
}

export interface BulletinActor {
  id: string;
  kind: string;
  seat_node_id: string | null;
  instruction_title: string;
  state: "working" | "waiting" | "needs_you" | "done";
  last_post_at: string | null;
}

export interface BulletinPage {
  roster: BulletinActor[];
  posts: BulletinPost[];
  next_cursor: string | null;
  total: number;
}

/** Replies whose parent is outside this page remain visible. */
export function bulletinThreads(posts: readonly BulletinPost[]) {
  const ids = new Set(posts.map((post) => post.post_id));
  return posts
    .filter((post) => !post.reply_to || !ids.has(post.reply_to))
    .map((post) => ({
      post,
      replies: posts.filter((reply) => reply.reply_to === post.post_id),
    }));
}

export function bulletinAuthor(post: BulletinPost): string {
  return post.author.kind === "member"
    ? (post.author.display_name ?? post.author.user_id ?? "Member")
    : (post.author.actor_label ?? post.author.operation_id ?? "Agent");
}
