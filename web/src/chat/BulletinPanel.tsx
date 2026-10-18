import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  Check,
  ChevronDown,
  CornerUpLeft,
  GitBranch,
  Hash,
  Pencil,
  RotateCw,
  Send,
  Trash2,
  Users,
  X,
} from "lucide-react";
import { api } from "../core/api";
import { graphTargetUrl, projectViewHash, sameGraphTarget } from "../core/graphTarget";
import { resourceKeyId, type ResourceProfile } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import type { GraphNode, GraphTargetRef } from "../core/types";
import { projectResourceKey, useProjectResource } from "../projects/projectResources";
import {
  bulletinActorKind,
  bulletinActorLabel,
  bulletinAuthor,
  bulletinChannelTarget,
  bulletinDecodeMentions,
  bulletinEncodeMentions,
  bulletinFilter,
  bulletinInsert,
  bulletinIsOwnPost,
  bulletinLabelMap,
  bulletinMentionCandidates,
  bulletinOrderChannels,
  bulletinPostState,
  bulletinReplyContext,
  bulletinSegments,
  bulletinTargetKey,
  bulletinThreads,
  bulletinTime,
  bulletinTrigger,
  type BulletinChannel,
  type BulletinPage,
  type BulletinPost,
} from "./bulletinModel";
import "./bulletin.css";

interface Props {
  projectId: string;
  /** The workspace's graph target, which is the channel shown; picking another channel switches it. */
  target: GraphTargetRef;
  /** The signed-in member; null when no verified session exists. */
  actorId: string | null;
  nodes?: Record<string, Pick<GraphNode, "id" | "title">>;
  onOpenNode?: (nodeId: string) => void;
}

interface Suggestion {
  key: string;
  label: string;
  detail?: string;
  icon: "member" | "agent" | "node";
  insert: string;
}

interface Failure {
  message: string;
  retry: () => void;
}

const NO_NODES: Record<string, Pick<GraphNode, "id" | "title">> = {};

export function BulletinPanel({ projectId, target, actorId, nodes = NO_NODES, onOpenNode }: Props) {
  const { store } = useRefreshRuntime();
  const isMember = actorId !== null;
  // The channel is the workspace's graph target, so node links and suggestions
  // resolve in the graph the channel belongs to.
  const channel = target;
  const [pickerOpen, setPickerOpen] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [caret, setCaret] = useState(0);
  const [focused, setFocused] = useState(false);
  const [dismissedAt, setDismissedAt] = useState<number | null>(null);
  const [active, setActive] = useState(0);
  const [rosterOpen, setRosterOpen] = useState(false);
  const [openThreads, setOpenThreads] = useState<Set<string>>(new Set());
  const [reply, setReply] = useState<BulletinPost | null>(null);
  const [editing, setEditing] = useState<{ postId: string; text: string; key: string } | null>(
    null,
  );
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const pendingCaret = useRef<number | null>(null);
  const marked = useRef(new Map<string, string>());
  const picker = useRef<HTMLDivElement>(null);

  const base = `/api/projects/${encodeURIComponent(projectId)}/bulletin`;
  const url = graphTargetUrl(base, channel);
  const channelKey = bulletinTargetKey(channel);
  const profile: ResourceProfile<BulletinPage> = {
    key: projectResourceKey(projectId, "bulletin", { query: `${url}:${cursor ?? ""}` }),
    fetch: (signal) =>
      api(
        `${url}${cursor ? `${url.includes("?") ? "&" : "?"}cursor=${encodeURIComponent(cursor)}` : ""}`,
        { signal },
      ),
  };
  // The channel list shares the project-wide bulletin watermark, so any post refreshes it.
  const channelsProfile: ResourceProfile<BulletinChannel[]> = {
    key: projectResourceKey(projectId, "bulletin", { query: `${base}/channels` }),
    fetch: (signal) => api(`${base}/channels`, { signal }),
  };
  const page = useProjectResource(profile);
  const channelList = useProjectResource(channelsProfile);
  const channels = useMemo(
    () => bulletinOrderChannels(channelList.value ?? []),
    [channelList.value],
  );
  const current = channels.find((item) => item.target_key === channelKey);
  const otherUnread = channels.some((item) => item.target_key !== channelKey && item.unread > 0);
  const canPost = isMember && page.value?.can_post === true;
  const roster = useMemo(() => page.value?.roster ?? [], [page.value]);
  const posts = useMemo(() => page.value?.posts ?? [], [page.value]);
  const members = useMemo(() => page.value?.members ?? [], [page.value]);
  const candidates = useMemo(() => bulletinMentionCandidates(members, roster), [members, roster]);
  const labels = useMemo(() => bulletinLabelMap(candidates), [candidates]);
  const resourceId = resourceKeyId(profile.key);
  const channelsId = resourceKeyId(channelsProfile.key);
  const reload = () => {
    void store.invalidate(resourceId);
    void store.invalidate(channelsId);
  };
  // Newest sits at the bottom beside the composer, as in a chat channel.
  const threads = bulletinThreads(posts).reverse();
  const newest = posts[0]?.post_id;

  useEffect(() => {
    if (!cursor && feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  }, [newest, cursor, channelKey]);
  useEffect(() => {
    const box = input.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${box.scrollHeight}px`;
    if (pendingCaret.current !== null) {
      box.setSelectionRange(pendingCaret.current, pendingCaret.current);
      pendingCaret.current = null;
    }
  }, [draft]);
  useEffect(() => {
    if (!pickerOpen) return;
    const close = (event: PointerEvent) => {
      if (!picker.current?.contains(event.target as Node)) setPickerOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [pickerOpen]);
  // Showing a channel's newest page marks the channel read up to its latest post,
  // including a reply in an older thread, as opening a chat channel does.
  const readUpTo = cursor ? null : (page.value?.newest_post_id ?? null);
  useEffect(() => {
    if (!isMember || !readUpTo || marked.current.get(channelKey) === readUpTo) return;
    marked.current.set(channelKey, readUpTo);
    api(graphTargetUrl(`${base}/read`, channel), {
      method: "POST",
      body: JSON.stringify({ sequence_post_id: readUpTo }),
    })
      .then(() => store.invalidate(channelsId))
      .catch(() => marked.current.delete(channelKey));
    // The channel key identifies the target; base and ids follow from it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMember, readUpTo, channelKey]);

  const trigger = canPost && focused ? bulletinTrigger(draft, caret) : null;
  const shownTrigger = trigger && trigger.start !== dismissedAt ? trigger : null;
  const nodeList = useMemo(() => Object.values(nodes), [nodes]);
  const nodeIds = useMemo(() => new Set(Object.keys(nodes)), [nodes]);
  const suggestions: Suggestion[] = !shownTrigger
    ? []
    : shownTrigger.char === "@"
      ? bulletinFilter(candidates, shownTrigger.query, (item) => [item.label]).map((item) => ({
          key: item.token,
          label: item.label,
          icon: item.mention.kind,
          insert: `@${item.label}`,
        }))
      : bulletinFilter(nodeList, shownTrigger.query, (node) => [node.id, node.title]).map(
          (node) => ({
            key: node.id,
            label: node.id,
            detail: node.title,
            icon: "node" as const,
            insert: `#${node.id}`,
          }),
        );
  const activeIndex = Math.min(active, Math.max(suggestions.length - 1, 0));

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setFailure(null);
    try {
      await action();
    } catch (error) {
      setFailure({
        message: error instanceof Error ? error.message : String(error),
        retry: () => void run(action),
      });
    } finally {
      setBusy(false);
    }
  };
  const post = () => {
    if (!draft.trim() || busy || !canPost) return;
    // Captured once so a retry resends the same keyed request.
    const body = JSON.stringify({
      text: bulletinEncodeMentions(draft, candidates).trimEnd(),
      key,
      reply_to: reply?.post_id ?? null,
    });
    const parent = reply?.post_id ?? null;
    void run(async () => {
      await api(url, { method: "POST", body });
      if (parent) setOpenThreads((open) => new Set(open).add(parent));
      setDraft("");
      setReply(null);
      setKey(crypto.randomUUID());
      // A reply stays on its thread's page; a new post lands on the newest page.
      if (!parent) setCursor(null);
      reload();
    });
  };
  const saveEdit = () => {
    if (!editing || !editing.text.trim() || busy) return;
    const target = graphTargetUrl(`${base}/${encodeURIComponent(editing.postId)}`, channel);
    const body = JSON.stringify({
      text: bulletinEncodeMentions(editing.text, candidates).trimEnd(),
      key: editing.key,
    });
    void run(async () => {
      await api(target, { method: "PATCH", body });
      setEditing(null);
      reload();
    });
  };
  const remove = (postId: string) => {
    if (confirmDelete !== postId) {
      setConfirmDelete(postId);
      return;
    }
    setConfirmDelete(null);
    const target = graphTargetUrl(`${base}/${encodeURIComponent(postId)}`, channel);
    void run(async () => {
      await api(target, { method: "DELETE" });
      if (reply?.post_id === postId) setReply(null);
      reload();
    });
  };
  const selectChannel = (next: GraphTargetRef) => {
    setPickerOpen(false);
    if (sameGraphTarget(next, channel)) return;
    window.location.hash = projectViewHash(projectId, next, "chats");
  };
  const startReply = (item: BulletinPost) => {
    setReply(item);
    setKey(crypto.randomUUID());
    input.current?.focus();
  };
  const startEdit = (item: BulletinPost) => {
    setConfirmDelete(null);
    setEditing({
      postId: item.post_id,
      text: bulletinDecodeMentions(item.text, candidates),
      key: crypto.randomUUID(),
    });
  };
  const pick = (suggestion: Suggestion) => {
    if (!shownTrigger) return;
    const next = bulletinInsert(draft, shownTrigger, caret, suggestion.insert);
    pendingCaret.current = next.caret;
    setDraft(next.text);
    setCaret(next.caret);
    setActive(0);
    setKey(crypto.randomUUID());
  };
  const toggleThread = (postId: string) =>
    setOpenThreads((open) => {
      const next = new Set(open);
      if (!next.delete(postId)) next.add(postId);
      return next;
    });

  const renderText = (item: BulletinPost) =>
    bulletinSegments(item.text, labels, nodeIds, item.mentions).map((segment, index) => {
      if (segment.kind === "text") return segment.text;
      if (segment.kind === "node")
        return (
          <button
            key={index}
            type="button"
            className="bulletin-node"
            onClick={() => onOpenNode?.(segment.id)}
          >
            #{segment.id}
          </button>
        );
      return (
        <span key={index} className="bulletin-mention" title={segment.mention.id}>
          @{segment.label}
        </span>
      );
    });

  const renderChannel = (item: BulletinChannel) => {
    const selected = item.target_key === channelKey;
    return (
      <li key={item.target_key}>
        <button
          type="button"
          className={`bulletin-channel${item.unread > 0 && !selected ? " is-unread" : ""}`}
          aria-current={selected ? "true" : undefined}
          title={item.name}
          onClick={() => selectChannel(bulletinChannelTarget(item))}
        >
          {item.branch_id ? <GitBranch size={13} /> : <Hash size={13} />}
          <span className="bulletin-channel-name">{item.name}</span>
          {item.unread > 0 && !selected && (
            <span className="bulletin-unread" aria-label={`${item.unread} unread`}>
              {item.unread}
            </span>
          )}
        </button>
      </li>
    );
  };

  const renderPost = (item: BulletinPost, replies: readonly BulletinPost[] | null) => {
    const state = bulletinPostState(item);
    const open = openThreads.has(item.post_id);
    // Threads are one level deep: a reply shown alone (parent on an older page) takes none.
    const canReply = replies !== null && canPost && item.reply_to === null;
    // The server counts the whole thread; a page may hold only some replies.
    const replyCount = Math.max(item.reply_count ?? 0, replies?.length ?? 0);
    const thread = replies && replyCount > 0 && (
      <>
        <button
          type="button"
          className="bulletin-thread-toggle"
          aria-expanded={open}
          onClick={() => toggleThread(item.post_id)}
        >
          {replyCount} {replyCount === 1 ? "reply" : "replies"}
        </button>
        {open && (
          <div className="bulletin-thread">
            {[...replies].reverse().map((child) => renderPost(child, null))}
          </div>
        )}
      </>
    );
    if (state === "deleted")
      return (
        <article
          className={`bulletin-message is-deleted${replies === null ? " is-reply" : ""}`}
          key={item.post_id}
          id={`bulletin-${item.post_id}`}
        >
          <span className="bulletin-avatar" aria-hidden />
          <div className="bulletin-message-main">
            <p className="bulletin-deleted">Post deleted</p>
            {thread}
          </div>
        </article>
      );
    const agent = item.author.kind === "agent";
    const author = bulletinAuthor(item);
    // A thread child sits under its parent; only a reply whose parent is on an
    // older page quotes it.
    const parent = replies !== null && item.reply_to ? bulletinReplyContext(item, posts) : null;
    const own = canPost && bulletinIsOwnPost(item, actorId);
    const isEditing = editing?.postId === item.post_id;
    const confirming = confirmDelete === item.post_id;
    return (
      <article
        className={`bulletin-message${replies === null ? " is-reply" : ""}`}
        key={item.post_id}
        id={`bulletin-${item.post_id}`}
        onMouseLeave={confirming ? () => setConfirmDelete(null) : undefined}
      >
        <span className={`bulletin-avatar${agent ? " is-agent" : ""}`} aria-hidden>
          {agent ? <Bot size={16} /> : author.trim().charAt(0).toUpperCase() || "?"}
        </span>
        <div className="bulletin-message-main">
          <header>
            <strong>{author}</strong>
            <time dateTime={item.created_at} title={new Date(item.created_at).toLocaleString()}>
              {bulletinTime(item.created_at)}
            </time>
            {state === "edited" && item.edited_at && (
              <span className="bulletin-edited" title={new Date(item.edited_at).toLocaleString()}>
                · edited
              </span>
            )}
          </header>
          {parent && (
            <p className="bulletin-quote">
              <CornerUpLeft size={12} /> {parent.author}:{" "}
              {bulletinDecodeMentions(parent.excerpt, candidates)}
            </p>
          )}
          {isEditing ? (
            <div className="bulletin-edit">
              <textarea
                aria-label="Edit post"
                value={editing.text}
                disabled={busy}
                autoFocus
                rows={Math.min(8, editing.text.split("\n").length)}
                onChange={(event) =>
                  setEditing({ ...editing, text: event.target.value, key: crypto.randomUUID() })
                }
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    setEditing(null);
                  } else if (
                    event.key === "Enter" &&
                    !event.shiftKey &&
                    !event.nativeEvent.isComposing
                  ) {
                    event.preventDefault();
                    saveEdit();
                  }
                }}
              />
              <button
                type="button"
                className="bulletin-icon"
                aria-label="Save edit"
                disabled={busy || !editing.text.trim()}
                onClick={saveEdit}
              >
                <Check size={15} />
              </button>
              <button
                type="button"
                className="bulletin-icon"
                aria-label="Cancel edit"
                onClick={() => setEditing(null)}
              >
                <X size={15} />
              </button>
            </div>
          ) : (
            <div className="bulletin-text">{renderText(item)}</div>
          )}
          {thread}
        </div>
        {!isEditing && (canReply || own) && (
          <div className="bulletin-message-actions">
            {canReply && (
              <button
                type="button"
                className="bulletin-message-action"
                aria-label={`Reply to ${author}`}
                onClick={() => startReply(item)}
              >
                <CornerUpLeft size={15} />
              </button>
            )}
            {own && (
              <button
                type="button"
                className="bulletin-message-action"
                aria-label="Edit post"
                onClick={() => startEdit(item)}
              >
                <Pencil size={14} />
              </button>
            )}
            {own && (
              <button
                type="button"
                className={`bulletin-message-action${confirming ? " is-confirming" : ""}`}
                aria-label={confirming ? "Confirm delete" : "Delete post"}
                onClick={() => remove(item.post_id)}
                onBlur={() => confirming && setConfirmDelete(null)}
              >
                <Trash2 size={14} />
              </button>
            )}
          </div>
        )}
      </article>
    );
  };

  const error = failure?.message ?? (page.error ? String(page.error) : null);
  return (
    <section className={`bulletin-panel${error ? " has-error" : ""}`} aria-label="Bulletin">
      {channels.length > 0 && (
        <nav className="bulletin-rail" aria-label="Channels">
          <ul>{channels.map(renderChannel)}</ul>
        </nav>
      )}
      <div className="bulletin-body">
        <header className="bulletin-head">
          {channels.length > 0 && (
            <div className="bulletin-picker" ref={picker}>
              <button
                type="button"
                className="bulletin-picker-button"
                aria-expanded={pickerOpen}
                aria-haspopup="listbox"
                onClick={() => setPickerOpen((value) => !value)}
              >
                {channel.kind === "branch" ? <GitBranch size={13} /> : <Hash size={13} />}
                <span className="bulletin-channel-name">
                  {current?.name ?? (channel.kind === "branch" ? channel.branch_id : "main")}
                </span>
                <ChevronDown size={14} />
                {otherUnread && <span className="bulletin-unread-dot" aria-label="Unread" />}
              </button>
              {pickerOpen && (
                <ul className="bulletin-picker-list">{channels.map(renderChannel)}</ul>
              )}
            </div>
          )}
          {error && (
            <button
              type="button"
              className="bulletin-icon"
              aria-label={failure ? "Retry" : "Retry refresh"}
              title={error}
              onClick={() => {
                if (failure) failure.retry();
                else reload();
              }}
            >
              <RotateCw size={15} />
            </button>
          )}
          <button
            type="button"
            className="bulletin-members"
            aria-expanded={rosterOpen && roster.length > 0}
            aria-label={`${roster.length} agents`}
            disabled={roster.length === 0}
            onClick={() => setRosterOpen((value) => !value)}
          >
            <Users size={14} />
            {roster.length + (page.value?.roster_omitted ?? 0)}
          </button>
        </header>
        {rosterOpen && roster.length > 0 && (
          <ul className="bulletin-roster">
            {roster.map((actor) => (
              <li key={actor.id} data-state={actor.state}>
                <span className="bulletin-state-dot" title={actor.state.replaceAll("_", " ")} />
                <span className="bulletin-roster-name" title={actor.id}>
                  {bulletinActorLabel(actor)}
                </span>
                <span className="bulletin-roster-kind">{bulletinActorKind(actor)}</span>
                {actor.seat_node_id && (
                  <button type="button" onClick={() => onOpenNode?.(actor.seat_node_id!)}>
                    {actor.seat_node_id}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        <div className="bulletin-feed" ref={feed}>
          {page.value?.next_cursor && (
            <button
              type="button"
              className="bulletin-page"
              onClick={() => setCursor(page.value!.next_cursor)}
            >
              Older
            </button>
          )}
          {threads.map(({ post: item, replies }) => renderPost(item, replies))}
          {cursor && (
            <button type="button" className="bulletin-page" onClick={() => setCursor(null)}>
              Newest
            </button>
          )}
        </div>
        {canPost && (
          <form
            className="bulletin-composer"
            onSubmit={(event) => {
              event.preventDefault();
              post();
            }}
          >
            {suggestions.length > 0 && (
              <ul className="bulletin-suggest" role="listbox" aria-label="Suggestions">
                {suggestions.map((suggestion, index) => (
                  <li
                    key={suggestion.key}
                    role="option"
                    aria-selected={index === activeIndex}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setActive(index)}
                    onClick={() => pick(suggestion)}
                  >
                    {suggestion.icon === "agent" ? (
                      <Bot size={14} />
                    ) : suggestion.icon === "node" ? (
                      <Hash size={14} />
                    ) : (
                      <span className="bulletin-suggest-initial" aria-hidden>
                        {suggestion.label.charAt(0).toUpperCase()}
                      </span>
                    )}
                    <span
                      className={`bulletin-suggest-label${suggestion.icon === "node" ? " is-node" : ""}`}
                    >
                      {suggestion.label}
                    </span>
                    {suggestion.detail && (
                      <span className="bulletin-suggest-detail">{suggestion.detail}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {reply && (
              <div className="bulletin-replying">
                <CornerUpLeft size={13} />
                <span>{bulletinAuthor(reply)}</span>
                <span className="bulletin-replying-text">
                  {bulletinDecodeMentions(reply.text, candidates)}
                </span>
                <button
                  type="button"
                  className="bulletin-icon"
                  aria-label="Cancel reply"
                  onClick={() => {
                    setReply(null);
                    setKey(crypto.randomUUID());
                  }}
                >
                  <X size={13} />
                </button>
              </div>
            )}
            <div className="bulletin-composer-row">
              <textarea
                ref={input}
                rows={1}
                aria-label="Bulletin post"
                placeholder={reply ? "Reply…" : "Message all agents"}
                value={draft}
                disabled={busy}
                onFocus={() => setFocused(true)}
                onBlur={() => setFocused(false)}
                onSelect={(event) => setCaret(event.currentTarget.selectionStart)}
                onChange={(event) => {
                  setDraft(event.target.value);
                  setCaret(event.target.selectionStart);
                  if (!bulletinTrigger(event.target.value, event.target.selectionStart))
                    setDismissedAt(null);
                  setActive(0);
                  setKey(crypto.randomUUID());
                }}
                onKeyDown={(event) => {
                  if (event.nativeEvent.isComposing) return;
                  if (suggestions.length > 0) {
                    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                      event.preventDefault();
                      const step = event.key === "ArrowDown" ? 1 : -1;
                      setActive((activeIndex + step + suggestions.length) % suggestions.length);
                      return;
                    }
                    if (event.key === "Enter" || event.key === "Tab") {
                      event.preventDefault();
                      pick(suggestions[activeIndex]);
                      return;
                    }
                    if (event.key === "Escape") {
                      event.preventDefault();
                      setDismissedAt(shownTrigger?.start ?? null);
                      return;
                    }
                  }
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    post();
                  }
                }}
              />
              <button
                className="bulletin-send"
                type="submit"
                aria-label="Post"
                disabled={busy || !draft.trim()}
              >
                <Send size={15} />
              </button>
            </div>
          </form>
        )}
      </div>
    </section>
  );
}
