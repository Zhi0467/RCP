import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Bot,
  Check,
  ChevronDown,
  CornerUpLeft,
  GitBranch,
  Hash,
  Paperclip,
  Pencil,
  RotateCw,
  Send,
  Users,
  X,
} from "lucide-react";
import { openArtifact } from "../artifacts/artifactViewerModel";
import { ReferenceIcon } from "../core/ReferenceChip";
import { ProjectReferencePicker } from "./ProjectReferencePicker";
import { api } from "../core/api";
import { graphTargetUrl, projectViewHash, sameGraphTarget } from "../core/graphTarget";
import { buildNotificationLink } from "../core/notificationLinks";
import { resourceKeyId, type ResourceProfile } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import { ArmedDeleteButton } from "../ui/ArmedDeleteButton";
import type { GraphNode, GraphTargetRef } from "../core/types";
import { projectResourceKey, useProjectResource } from "../projects/projectResources";
import {
  bulletinActorKind,
  bulletinActorLabel,
  bulletinAgentChoices,
  bulletinAuthor,
  bulletinChannelTarget,
  bulletinDate,
  bulletinDecodeMentions,
  bulletinEncodeMentions,
  bulletinFilter,
  bulletinInsert,
  bulletinIsOwnPost,
  bulletinKeyTarget,
  bulletinLabelMap,
  bulletinChannelCandidates,
  bulletinMentionKind,
  bulletinMoveDraft,
  bulletinPostState,
  bulletinSegments,
  bulletinSuggestPlacement,
  bulletinTargetKey,
  bulletinThreads,
  bulletinTime,
  bulletinTrigger,
  type BulletinActor,
  type BulletinChannel,
  type BulletinPage,
  type BulletinPost,
} from "./bulletinModel";
import "./bulletin.css";

interface Props {
  projectId: string;
  /** The workspace's graph target, the channel shown first; picking another channel never changes it. */
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
  /** Values the typed query matches. */
  match: readonly string[];
}

interface Failure {
  message: string;
  retry: () => void;
}

const NO_NODES: Record<string, Pick<GraphNode, "id" | "title">> = {};
// The phone shell pins these above the scrolling page.
const PINNED_BARS = ".phone-project-bar, .phone-tab-bar";

export function BulletinPanel({ projectId, target, actorId, nodes = NO_NODES, onOpenNode }: Props) {
  const { store } = useRefreshRuntime();
  const isMember = actorId !== null;
  // The Bulletin's own channel: it starts at the workspace's graph target and
  // switches without moving the workspace to another branch.
  const [channel, setChannel] = useState<GraphTargetRef>(target);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [artifactPickerOpen, setArtifactPickerOpen] = useState(false);
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
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const editBox = useRef<HTMLTextAreaElement>(null);
  const pendingCaret = useRef<number | null>(null);
  const marked = useRef(new Map<string, string>());
  const picker = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLFormElement>(null);

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
  const channels = channelList.value ?? [];
  const current = channels.find((item) => item.target_key === channelKey);
  const otherUnread = channels.some((item) => item.target_key !== channelKey && item.unread > 0);
  // Roster and members are project-wide, so while another channel loads the last
  // page's stay in use and the composer, with its draft and focus, stays put.
  const [lastPage, setLastPage] = useState(page.value);
  if (page.value && page.value !== lastPage) setLastPage(page.value);
  const known = page.value ?? lastPage;
  const canPost = isMember && known?.can_post === true;
  // The roster holds every agent in the project; the shown channel's are the ones it mentions.
  const roster = useMemo(() => known?.roster ?? [], [known]);
  const channelRoster = useMemo(
    () => roster.filter((actor) => actor.target_key === channelKey),
    [roster, channelKey],
  );
  const posts = useMemo(() => page.value?.posts ?? [], [page.value]);
  const members = useMemo(() => known?.members ?? [], [known]);
  const candidates = useMemo(
    () => bulletinChannelCandidates(members, roster, channelKey),
    [members, roster, channelKey],
  );
  const labels = useMemo(() => bulletinLabelMap(candidates), [candidates]);
  // A roster refresh can shift labels (a renamed agent frees its name for a
  // same-named one), so open text is rebased to keep each mention's recipient.
  // A channel switch moves the draft itself.
  const shownCandidates = useRef({ channelKey, candidates });
  useEffect(() => {
    const previous = shownCandidates.current;
    shownCandidates.current = { channelKey, candidates };
    if (
      previous.channelKey !== channelKey ||
      (previous.candidates.length === candidates.length &&
        previous.candidates.every(
          (item, index) =>
            item.token === candidates[index].token && item.label === candidates[index].label,
        ))
    )
      return;
    const rebase = (text: string) => bulletinMoveDraft(text, previous.candidates, candidates);
    setDraft(rebase);
    setEditing((current) => current && { ...current, text: rebase(current.text) });
  }, [channelKey, candidates]);
  const agentNames = useMemo(
    () => new Map(roster.map((actor) => [actor.id, actor.name])),
    [roster],
  );
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
  // Opening an edit puts the caret after the text; autoFocus leaves it at the start.
  const editingPost = editing?.postId;
  useEffect(() => {
    const box = editBox.current;
    if (!editingPost || !box) return;
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  }, [editingPost]);
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
  // Node links open the workspace's graph, so another channel's posts show ids as text.
  const workspaceChannel = sameGraphTarget(channel, target);
  const channelNodes = workspaceChannel ? nodes : NO_NODES;
  const nodeList = useMemo(() => Object.values(channelNodes), [channelNodes]);
  const nodeIds = useMemo(() => new Set(Object.keys(channelNodes)), [channelNodes]);
  const channelName = (targetKey: string) =>
    channels.find((item) => item.target_key === targetKey)?.name ??
    (targetKey === "main" ? "main" : targetKey.slice("branch:".length));
  const mentionOptions = (): Suggestion[] => [
    ...candidates.slice(0, members.length).map((item) => ({
      key: item.token,
      label: item.label,
      icon: "member" as const,
      insert: `@${item.label}`,
      match: [item.label],
    })),
    ...bulletinAgentChoices(members, roster, channelKey).map(({ actor, label, token }) => ({
      key: token,
      label,
      detail: [
        bulletinMentionKind(actor),
        actor.seat_node_id,
        bulletinDate(actor.started_at),
        actor.target_key === channelKey ? null : channelName(actor.target_key),
      ]
        .filter(Boolean)
        .join(" · "),
      icon: "agent" as const,
      insert: `@${label}`,
      match: [label, actor.name, actor.seat_node_id ?? "", bulletinMentionKind(actor) ?? ""],
    })),
  ];
  const suggestions: Suggestion[] = !shownTrigger
    ? []
    : bulletinFilter(
        shownTrigger.char === "@"
          ? mentionOptions()
          : nodeList.map((node) => ({
              key: node.id,
              label: node.id,
              detail: node.title,
              icon: "node" as const,
              insert: `#${node.id}`,
              match: [node.id, node.title],
            })),
        shownTrigger.query,
        (item) => item.match,
      );
  const suggestOpen = suggestions.length > 0 || artifactPickerOpen;
  const [suggestPlace, setSuggestPlace] = useState<ReturnType<typeof bulletinSuggestPlacement>>();
  // Measured before paint, and again on scroll or resize, so the list never runs
  // past the scroll area's edge or under the phone's pinned bars.
  useLayoutEffect(() => {
    const form = composer.current;
    if (!suggestOpen || !form) return;
    const measure = () => {
      let area: Element | null = form.parentElement;
      while (area && !/(auto|scroll)/.test(getComputedStyle(area).overflowY))
        area = area.parentElement;
      const box = form.getBoundingClientRect();
      const bounds = area?.getBoundingClientRect() ?? { top: 0, bottom: window.innerHeight };
      let top = Math.max(0, bounds.top);
      let bottom = Math.min(window.innerHeight, bounds.bottom);
      for (const bar of document.querySelectorAll(PINNED_BARS)) {
        const edge = bar.getBoundingClientRect();
        if (edge.height === 0) continue;
        if (edge.bottom <= box.top) top = Math.max(top, edge.bottom);
        else if (edge.top >= box.bottom) bottom = Math.min(bottom, edge.top);
      }
      const next = bulletinSuggestPlacement(box, { top, bottom });
      setSuggestPlace((shown) =>
        shown?.side === next.side && shown.maxHeight === next.maxHeight ? shown : next,
      );
    };
    measure();
    window.addEventListener("resize", measure);
    document.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      document.removeEventListener("scroll", measure, true);
    };
  }, [suggestOpen, suggestions.length, artifactPickerOpen]);
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
    const submitted = draft;
    void run(async () => {
      const saved = await api<{ post: BulletinPost }>(url, { method: "POST", body });
      // The accepted post shows at once; the confirming read follows over the network.
      const shown = store.committedValue<BulletinPage>(resourceId);
      if (shown && (parent || !cursor)) {
        const fresh = !shown.posts.some((item) => item.post_id === saved.post.post_id);
        store.write(resourceId, {
          ...shown,
          posts: [
            saved.post,
            ...shown.posts
              .filter((item) => item.post_id !== saved.post.post_id)
              .map((item) =>
                fresh && item.post_id === parent
                  ? { ...item, reply_count: (item.reply_count ?? 0) + 1 }
                  : item,
              ),
          ],
          newest_post_id: saved.post.post_id,
          total: fresh && !parent ? shown.total + 1 : shown.total,
        });
      }
      if (parent) setOpenThreads((open) => new Set(open).add(parent));
      // A retry after the member revised the draft keeps the newer text unsent.
      if ((input.current?.value ?? submitted) === submitted) {
        setDraft("");
        setReply(null);
      }
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
    const submitted = editing;
    void run(async () => {
      await api(target, { method: "PATCH", body });
      // A retry after the member revised the edit keeps the newer text open.
      setEditing((current) =>
        current?.postId === submitted.postId && current.text === submitted.text ? null : current,
      );
      reload();
    });
  };
  const remove = (postId: string) => {
    const target = graphTargetUrl(`${base}/${encodeURIComponent(postId)}`, channel);
    void run(async () => {
      await api(target, { method: "DELETE" });
      if (reply?.post_id === postId) setReply(null);
      reload();
    });
  };
  const channelCandidates = (targetKey: string) =>
    bulletinChannelCandidates(members, roster, targetKey);
  const switchChannel = (next: GraphTargetRef) => {
    setArtifactPickerOpen(false);
    setChannel(next);
    setCursor(null);
    setReply(null);
    setEditing(null);
    setFailure(null);
    setKey(crypto.randomUUID());
  };
  const selectChannel = (next: GraphTargetRef) => {
    setPickerOpen(false);
    // A post in flight settles against the channel it was sent to.
    if (busy || sameGraphTarget(next, channel)) return;
    setDraft(bulletinMoveDraft(draft, candidates, channelCandidates(bulletinTargetKey(next))));
    switchChannel(next);
  };
  const startReply = (item: BulletinPost) => {
    setReply(item);
    setKey(crypto.randomUUID());
    input.current?.focus();
  };
  const startEdit = (item: BulletinPost) => {
    setEditing({
      postId: item.post_id,
      text: bulletinDecodeMentions(item.text, candidates),
      key: crypto.randomUUID(),
    });
  };
  const pick = (suggestion: Suggestion) => {
    if (!shownTrigger) return;
    // A mention reaches its agent from any channel, so the draft stays here.
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

  // Another channel's graph is not loaded here; its server-read references open the
  // node on that channel's branch, as a notification link does.
  const openNode = (nodeId: string) => {
    if (workspaceChannel) onOpenNode?.(nodeId);
    else
      window.location.hash = buildNotificationLink({
        projectId,
        target: channel.kind === "branch" ? channel.branch_id : "main",
        kind: "node",
        itemId: nodeId,
      });
  };
  const openAgent = (actor: BulletinActor) => {
    window.location.hash = projectViewHash(
      projectId,
      bulletinKeyTarget(actor.target_key),
      "chats",
      {
        chatId: actor.chat_id,
      },
    );
  };
  const rosterById = useMemo(() => new Map(roster.map((actor) => [actor.id, actor])), [roster]);
  const renderText = (item: BulletinPost) =>
    bulletinSegments(
      item.text,
      labels,
      workspaceChannel ? nodeIds : new Set(item.refs),
      item.mentions,
      item.artifacts,
    ).map((segment, index) => {
      if (segment.kind === "text") return segment.text;
      if (segment.kind === "artifact")
        return (
          <button
            key={index}
            type="button"
            className="bulletin-artifact"
            title={segment.artifact.source_name}
            onClick={() => openArtifact({ projectId, artifactId: segment.artifact.artifact_id })}
          >
            <ReferenceIcon kind="artifact" />
            <span>{segment.artifact.source_name}</span>
          </button>
        );
      if (segment.kind === "node")
        return (
          <button
            key={index}
            type="button"
            className="bulletin-node"
            onClick={() => openNode(segment.id)}
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
    const author = bulletinAuthor(item, agentNames);
    // An agent still on the roster opens its conversation from its name or avatar.
    const authorActor = agent ? rosterById.get(item.author.actor_id ?? "") : undefined;
    const own = canPost && bulletinIsOwnPost(item, actorId);
    const isEditing = editing?.postId === item.post_id;
    return (
      <article
        className={`bulletin-message${replies === null ? " is-reply" : ""}`}
        key={item.post_id}
        id={`bulletin-${item.post_id}`}
      >
        {authorActor ? (
          <button
            type="button"
            className="bulletin-avatar is-agent"
            aria-hidden
            tabIndex={-1}
            onClick={() => openAgent(authorActor)}
          >
            <Bot size={16} />
          </button>
        ) : (
          <span className={`bulletin-avatar${agent ? " is-agent" : ""}`} aria-hidden>
            {agent ? <Bot size={16} /> : author.trim().charAt(0).toUpperCase() || "?"}
          </span>
        )}
        <div className="bulletin-message-main">
          <header>
            {authorActor ? (
              <button
                type="button"
                className="bulletin-author"
                onClick={() => openAgent(authorActor)}
              >
                {author}
              </button>
            ) : (
              <strong>{author}</strong>
            )}
            <time dateTime={item.created_at} title={new Date(item.created_at).toLocaleString()}>
              {bulletinTime(item.created_at)}
            </time>
            {state === "edited" && item.edited_at && (
              <span className="bulletin-edited" title={new Date(item.edited_at).toLocaleString()}>
                · edited
              </span>
            )}
          </header>
          {isEditing ? (
            <div className="bulletin-edit">
              <textarea
                ref={editBox}
                aria-label="Edit post"
                value={editing.text}
                disabled={busy}
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
              <ArmedDeleteButton
                label="Delete post"
                className="bulletin-message-action"
                onConfirm={() => remove(item.post_id)}
              />
            )}
          </div>
        )}
      </article>
    );
  };

  const error = failure?.message ?? (page.error ? String(page.error) : null);
  // The first read draws the panel whole; until then there is nothing to frame.
  if (!known && !error) return null;
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
            aria-expanded={rosterOpen && channelRoster.length > 0}
            aria-label={`${channelRoster.length} ${channelRoster.length === 1 ? "agent" : "agents"}`}
            disabled={channelRoster.length === 0}
            onClick={() => setRosterOpen((value) => !value)}
          >
            <Users size={14} />
            {channelRoster.length}
          </button>
        </header>
        {rosterOpen && channelRoster.length > 0 && (
          <ul className="bulletin-roster">
            {channelRoster.map((actor) => (
              <li key={actor.id} data-state={actor.state}>
                <span className="bulletin-state-dot" title={actor.state.replaceAll("_", " ")} />
                <button
                  type="button"
                  className="bulletin-roster-name"
                  title={actor.id}
                  onClick={() => openAgent(actor)}
                >
                  {bulletinActorLabel(actor)}
                </button>
                <span className="bulletin-roster-kind">{bulletinActorKind(actor)}</span>
                {actor.seat_node_id && (
                  <button type="button" onClick={() => openNode(actor.seat_node_id!)}>
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
            ref={composer}
            className="bulletin-composer"
            onSubmit={(event) => {
              event.preventDefault();
              post();
            }}
          >
            {artifactPickerOpen && (
              <div
                className={`bulletin-artifact-picker${suggestPlace?.side === "below" ? " below" : ""}`}
                style={suggestPlace ? { maxHeight: suggestPlace.maxHeight } : undefined}
              >
                <ProjectReferencePicker
                  projectId={projectId}
                  target={channel}
                  nodes={{}}
                  artifactsOnly
                  selectedKeys={new Set()}
                  onClose={() => {
                    setArtifactPickerOpen(false);
                    input.current?.focus();
                  }}
                  onPick={(reference) => {
                    if (reference.selector.kind !== "artifact") return;
                    const token = `<artifact:${reference.selector.artifact_id}>`;
                    const head = draft.slice(0, caret);
                    const insertion = `${head && !/\s$/.test(head) ? " " : ""}${token} `;
                    const next = `${head}${insertion}${draft.slice(caret)}`;
                    pendingCaret.current = head.length + insertion.length;
                    setDraft(next);
                    setCaret(pendingCaret.current);
                    setKey(crypto.randomUUID());
                    setArtifactPickerOpen(false);
                    input.current?.focus();
                  }}
                />
              </div>
            )}
            {!artifactPickerOpen && suggestions.length > 0 && (
              <ul
                className={`bulletin-suggest${suggestPlace?.side === "below" ? " below" : ""}`}
                style={suggestPlace ? { maxHeight: suggestPlace.maxHeight } : undefined}
                role="listbox"
                aria-label="Suggestions"
              >
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
                <span>{bulletinAuthor(reply, agentNames)}</span>
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
              <button
                type="button"
                className="bulletin-icon bulletin-attach"
                aria-label="Attach artifact"
                aria-expanded={artifactPickerOpen}
                disabled={busy}
                onClick={() => setArtifactPickerOpen((open) => !open)}
              >
                <Paperclip size={15} />
              </button>
              <textarea
                ref={input}
                rows={1}
                aria-label="Bulletin post"
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
