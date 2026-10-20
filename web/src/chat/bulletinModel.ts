import type { GraphTargetRef } from "../core/types";

export interface BulletinMention {
  kind: "member" | "agent";
  id: string;
}

export interface BulletinPost {
  post_id: string;
  author: {
    kind: "agent" | "member";
    operation_id?: string;
    actor_id?: string;
    actor_label?: string;
    user_id?: string;
    display_name?: string;
  };
  /** "" once deleted. */
  text: string;
  refs: string[];
  mentions?: BulletinMention[];
  reply_to: string | null;
  reply_parent?: Pick<BulletinPost, "post_id" | "author" | "text"> | null;
  created_at: string;
  edited_at?: string | null;
  deleted_at?: string | null;
  reply_count?: number;
}

/** A roster entry; the roster lists every agent in the project, each in its own channel. */
export interface BulletinActor {
  /** The mention id used in `<@agent:ID>` tokens. */
  id: string;
  chat_id: string;
  /** The agent's channel: "main" or "branch:<branch_id>". */
  target_key: string;
  /** The agent's name, the same one its Agents board card shows. */
  name: string;
  kind: string;
  seat_node_id: string | null;
  started_at: string;
  state: "working" | "waiting" | "needs_you" | "done";
  last_post_at: string | null;
}

export interface BulletinMember {
  user_id: string;
  display_name: string;
}

export interface BulletinPage {
  roster: BulletinActor[];
  can_post: boolean;
  posts: BulletinPost[];
  /** The channel's latest post anywhere, a reply in an older thread included. */
  newest_post_id: string | null;
  members?: BulletinMember[];
  next_cursor: string | null;
  total: number;
}

export interface BulletinChannel {
  target_key: string;
  branch_id: string | null;
  name: string;
  last_post_at: string | null;
  unread: number;
}

const ACTOR_KINDS: Record<string, string> = {
  human_chat_work: "Work",
  human_chat_discuss: "Discuss",
  auto_research_root: "Auto-research",
  worker_node: "Node worker",
  worker_project: "Project worker",
  experiment_loop: "Experiment",
};

export function bulletinActorKind(actor: Pick<BulletinActor, "kind">): string {
  return ACTOR_KINDS[actor.kind] || actor.kind;
}

/** A mention row names the kind only for autonomous agents; a conversation needs no Work/Discuss tag. */
export function bulletinMentionKind(actor: Pick<BulletinActor, "kind">): string | null {
  return actor.kind.startsWith("human_chat_") ? null : bulletinActorKind(actor);
}

export function bulletinActorLabel(actor: Pick<BulletinActor, "kind" | "name">) {
  return actor.name || bulletinActorKind(actor);
}

export function bulletinThreads(posts: readonly BulletinPost[]) {
  return posts
    .filter((post) => !post.reply_to)
    .map((post) => ({
      post,
      replies: posts.filter((reply) => reply.reply_to === post.post_id),
    }));
}

/** An agent author shows its current name; the label stored with the post covers agents no longer listed. */
export function bulletinAuthor(
  post: Pick<BulletinPost, "author">,
  names: ReadonlyMap<string, string> = new Map(),
): string {
  if (post.author.kind === "member")
    return post.author.display_name ?? post.author.user_id ?? "Member";
  const current = post.author.actor_id ? names.get(post.author.actor_id) : undefined;
  return current ?? post.author.actor_label ?? post.author.operation_id ?? "Agent";
}

export function bulletinDate(iso: string): string {
  return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });
}

/** Clock time for today's posts, a short date for older ones. */
export function bulletinTime(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  return at.toDateString() === now.toDateString()
    ? at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : bulletinDate(iso);
}

export function bulletinPostState(
  post: Pick<BulletinPost, "edited_at" | "deleted_at">,
): "deleted" | "edited" | "plain" {
  if (post.deleted_at) return "deleted";
  return post.edited_at ? "edited" : "plain";
}

/** A member edits and deletes only the posts it authored; agent posts stay the agent's. */
export function bulletinIsOwnPost(post: Pick<BulletinPost, "author">, userId: string | null) {
  return userId !== null && post.author.kind === "member" && post.author.user_id === userId;
}

// ---- Channels ----

export function bulletinChannelTarget(channel: Pick<BulletinChannel, "branch_id">): GraphTargetRef {
  return channel.branch_id ? { kind: "branch", branch_id: channel.branch_id } : { kind: "main" };
}

export function bulletinTargetKey(target: GraphTargetRef): string {
  return target.kind === "main" ? "main" : `branch:${target.branch_id}`;
}

export function bulletinKeyTarget(targetKey: string): GraphTargetRef {
  return targetKey.startsWith("branch:")
    ? { kind: "branch", branch_id: targetKey.slice("branch:".length) }
    : { kind: "main" };
}

// ---- Text segments ----

export type BulletinSegment =
  | { kind: "text"; text: string }
  | { kind: "node"; id: string }
  | { kind: "mention"; mention: BulletinMention; label: string };

const TOKEN = /<@(member|agent):([^>\s]+)>|#([A-Za-z0-9][A-Za-z0-9_./-]*)/g;
const NODE_TRAILING = /[.,;:)]+$/;

/**
 * Splits post text into prose, `#node` links, and mention tokens. A `#id`
 * links only when the node exists; a token renders as a pill when its name is
 * known or the server recorded it as a mention; anything else stays prose.
 */
export function bulletinSegments(
  text: string,
  labels: ReadonlyMap<string, string>,
  nodeIds: ReadonlySet<string>,
  mentions: readonly BulletinMention[] = [],
): BulletinSegment[] {
  const segments: BulletinSegment[] = [];
  const push = (value: string) => {
    if (!value) return;
    const last = segments[segments.length - 1];
    if (last?.kind === "text") last.text += value;
    else segments.push({ kind: "text", text: value });
  };
  let at = 0;
  for (const match of text.matchAll(TOKEN)) {
    const start = match.index;
    push(text.slice(at, start));
    if (match[3] !== undefined) {
      const id = match[3].replace(NODE_TRAILING, "");
      if (nodeIds.has(id)) segments.push({ kind: "node", id });
      else push(`#${id}`);
      at = start + 1 + id.length;
      continue;
    }
    const mention = { kind: match[1] as BulletinMention["kind"], id: match[2] };
    const label = labels.get(mentionToken(mention));
    const recorded = mentions.some((item) => item.kind === mention.kind && item.id === mention.id);
    if (label !== undefined || recorded) {
      segments.push({ kind: "mention", mention, label: label ?? mention.id });
    } else push(match[0]);
    at = start + match[0].length;
  }
  push(text.slice(at));
  return segments;
}

// ---- Mentions in the composer ----

export interface BulletinMentionCandidate {
  mention: BulletinMention;
  label: string;
  token: string;
}

export function mentionToken(mention: BulletinMention): string {
  return `<@${mention.kind}:${mention.id}>`;
}

/** Members then agents, each with a label unique across the list. */
export function bulletinMentionCandidates(
  members: readonly BulletinMember[],
  roster: readonly BulletinActor[],
): BulletinMentionCandidate[] {
  const raw = [
    ...members.map((member) => ({
      mention: { kind: "member" as const, id: member.user_id },
      label: member.display_name || member.user_id,
    })),
    ...roster.map((actor) => ({
      mention: { kind: "agent" as const, id: actor.id },
      label: bulletinActorLabel(actor),
    })),
  ];
  const used = new Set<string>();
  return raw.map(({ mention, label }) => {
    let unique = label;
    for (let n = 2; used.has(unique.toLowerCase()); n += 1) unique = `${label} ${n}`;
    used.add(unique.toLowerCase());
    return { mention, label: unique, token: mentionToken(mention) };
  });
}

export interface BulletinAgentChoice {
  actor: BulletinActor;
  /** The agent's `@` label among the members and agents of its own channel. */
  label: string;
  token: string;
}

/**
 * Every agent with the label it takes in its own channel, the shown channel's
 * agents first. Mentions count only in the agent's channel, so labels are made
 * unique per channel, exactly as that channel's composer encodes them.
 */
export function bulletinAgentChoices(
  members: readonly BulletinMember[],
  roster: readonly BulletinActor[],
  channelKey: string,
): BulletinAgentChoice[] {
  const keys = [channelKey, ...new Set(roster.map((actor) => actor.target_key))];
  return [...new Set(keys)].flatMap((key) => {
    const actors = roster.filter((actor) => actor.target_key === key);
    const candidates = bulletinMentionCandidates(members, actors).slice(members.length);
    return actors.map((actor, index) => ({
      actor,
      label: candidates[index].label,
      token: candidates[index].token,
    }));
  });
}

export function bulletinLabelMap(
  candidates: readonly BulletinMentionCandidate[],
): Map<string, string> {
  return new Map(candidates.map((candidate) => [candidate.token, candidate.label]));
}

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Readable `@Name` in the composer becomes the stored token on send. */
export function bulletinEncodeMentions(
  draft: string,
  candidates: readonly BulletinMentionCandidate[],
): string {
  if (candidates.length === 0) return draft;
  const ordered = [...candidates].sort((left, right) => right.label.length - left.label.length);
  const byLabel = new Map(ordered.map((candidate) => [candidate.label, candidate.token]));
  // A stored token is matched first and kept whole, so a label like `agent` never rewrites its inside.
  const pattern = new RegExp(
    `(<@(?:member|agent):[^>\\s]+>)|(^|[^\\w@])@(${ordered.map((candidate) => escapeRegExp(candidate.label)).join("|")})(?!\\w)`,
    "g",
  );
  return draft.replace(pattern, (whole, token: string | undefined, lead: string, label: string) =>
    token ? whole : `${lead}${byLabel.get(label)}`,
  );
}

/** Stored tokens become `@Name` for editing; unknown tokens stay as written. */
export function bulletinDecodeMentions(
  text: string,
  candidates: readonly BulletinMentionCandidate[],
): string {
  const labels = bulletinLabelMap(candidates);
  return text.replace(/<@(member|agent):([^>\s]+)>/g, (token) => {
    const label = labels.get(token);
    return label === undefined ? token : `@${label}`;
  });
}

/** Carries a draft to another channel: each mention keeps its recipient, shown by name where that channel knows it. */
export function bulletinMoveDraft(
  text: string,
  from: readonly BulletinMentionCandidate[],
  to: readonly BulletinMentionCandidate[],
): string {
  return bulletinDecodeMentions(bulletinEncodeMentions(text, from), to);
}

// ---- Autocomplete ----

export interface BulletinTrigger {
  char: "@" | "#";
  query: string;
  start: number;
}

/** The `@` or `#` word the caret is in, when it starts at a word boundary. */
export function bulletinTrigger(text: string, caret: number): BulletinTrigger | null {
  const before = text.slice(0, caret);
  const match = /(^|[\s(])([@#])([^\s@#]*)$/.exec(before);
  if (!match) return null;
  return {
    char: match[2] as "@" | "#",
    query: match[3],
    start: before.length - match[3].length - 1,
  };
}

/** Prefix matches first, then word-prefix, then substring; all case-insensitive. */
export function bulletinFilter<T>(
  items: readonly T[],
  query: string,
  keys: (item: T) => readonly string[],
  limit = 8,
): T[] {
  const needle = query.toLowerCase();
  const score = (item: T) => {
    if (!needle) return 1;
    const values = keys(item).map((value) => value.toLowerCase());
    if (values.some((value) => value.startsWith(needle))) return 3;
    if (values.some((value) => value.split(/[\s/_.-]+/).some((word) => word.startsWith(needle))))
      return 2;
    return values.some((value) => value.includes(needle)) ? 1 : 0;
  };
  return items
    .map((item, index) => ({ item, index, rank: score(item) }))
    .filter(({ rank }) => rank > 0)
    .sort((left, right) => right.rank - left.rank || left.index - right.index)
    .slice(0, limit)
    .map(({ item }) => item);
}

/** Replaces the trigger word before the caret with `insertion` and one space. */
export function bulletinInsert(
  text: string,
  trigger: BulletinTrigger,
  caret: number,
  insertion: string,
): { text: string; caret: number } {
  const head = `${text.slice(0, trigger.start)}${insertion} `;
  return { text: `${head}${text.slice(caret).replace(/^ /, "")}`, caret: head.length };
}

export const BULLETIN_SUGGEST_MAX_HEIGHT = 260;
const BULLETIN_SUGGEST_GUTTER = 8;

/** Open the suggestions on the composer's side with room, never past the visible scroll area. */
export function bulletinSuggestPlacement(
  composer: { top: number; bottom: number },
  area: { top: number; bottom: number },
): { side: "above" | "below"; maxHeight: number } {
  const above = composer.top - area.top - BULLETIN_SUGGEST_GUTTER;
  const below = area.bottom - composer.bottom - BULLETIN_SUGGEST_GUTTER;
  const side = above >= Math.min(BULLETIN_SUGGEST_MAX_HEIGHT, below) ? "above" : "below";
  const room = side === "above" ? above : below;
  return { side, maxHeight: Math.max(0, Math.min(BULLETIN_SUGGEST_MAX_HEIGHT, room)) };
}
