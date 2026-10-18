import { useEffect, useRef, useState } from "react";
import { Bot, CornerUpLeft, Link2, RotateCw, Send, Users, X } from "lucide-react";
import { api } from "../core/api";
import { graphTargetUrl } from "../core/graphTarget";
import { resourceKeyId, type ResourceProfile } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import type { GraphTargetRef } from "../core/types";
import { projectResourceKey, useProjectResource } from "../projects/projectResources";
import {
  bulletinAuthor,
  bulletinThreads,
  bulletinReplyContext,
  bulletinTime,
  type BulletinPage,
  type BulletinPost,
} from "./bulletinModel";
import "./bulletin.css";

interface Props {
  projectId: string;
  target: GraphTargetRef;
  isMember: boolean;
  onOpenNode?: (nodeId: string) => void;
}

const kinds: Record<string, string> = {
  human_chat_work: "Work",
  human_chat_discuss: "Discuss",
  auto_research_root: "Auto-research",
  worker_node: "Node worker",
  worker_project: "Project worker",
  experiment: "Experiment",
};

export function BulletinPanel({ projectId, target, isMember, onOpenNode }: Props) {
  const { store } = useRefreshRuntime();
  const [cursor, setCursor] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [refs, setRefs] = useState("");
  const [refsOpen, setRefsOpen] = useState(false);
  const [rosterOpen, setRosterOpen] = useState(false);
  const [openThreads, setOpenThreads] = useState<Set<string>>(new Set());
  const [reply, setReply] = useState<BulletinPost | null>(null);
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const feed = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const url = graphTargetUrl(`/api/projects/${encodeURIComponent(projectId)}/bulletin`, target);
  const profile: ResourceProfile<BulletinPage> = {
    key: projectResourceKey(projectId, "bulletin", { query: `${url}:${cursor ?? ""}` }),
    fetch: (signal) =>
      api(
        `${url}${cursor ? `${url.includes("?") ? "&" : "?"}cursor=${encodeURIComponent(cursor)}` : ""}`,
        { signal },
      ),
  };
  const page = useProjectResource(profile);
  const canPost = isMember && page.value?.can_post === true;
  const roster = page.value?.roster ?? [];
  const posts = page.value?.posts ?? [];
  const resourceId = resourceKeyId(profile.key);
  const reload = () => store.invalidate(resourceId);
  // Newest sits at the bottom beside the composer, as in a chat channel.
  const threads = bulletinThreads(posts).reverse();
  const newest = posts[0]?.post_id;
  useEffect(() => {
    if (!cursor && feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  }, [newest, cursor]);
  useEffect(() => {
    const box = input.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${box.scrollHeight}px`;
  }, [draft]);

  const post = async () => {
    if (!draft.trim() || busy || !canPost) return;
    setBusy(true);
    setError(null);
    try {
      await api(url, {
        method: "POST",
        body: JSON.stringify({
          text: draft,
          key,
          refs: refs
            .split(",")
            .map((ref) => ref.trim())
            .filter(Boolean),
          reply_to: reply?.post_id ?? null,
        }),
      });
      if (reply) setOpenThreads((open) => new Set(open).add(reply.post_id));
      setDraft("");
      setRefs("");
      setRefsOpen(false);
      setReply(null);
      setKey(crypto.randomUUID());
      setCursor(null);
      void reload();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  };
  const startReply = (item: BulletinPost) => {
    setReply(item);
    setKey(crypto.randomUUID());
    input.current?.focus();
  };
  const toggleThread = (postId: string) =>
    setOpenThreads((open) => {
      const next = new Set(open);
      if (!next.delete(postId)) next.add(postId);
      return next;
    });

  const renderPost = (item: BulletinPost, replies: readonly BulletinPost[] | null) => {
    const agent = item.author.kind === "agent";
    const author = bulletinAuthor(item);
    // A thread child sits under its parent; only a reply whose parent is on an
    // older page quotes it.
    const parent = replies !== null && item.reply_to ? bulletinReplyContext(item, posts) : null;
    const open = openThreads.has(item.post_id);
    return (
      <article
        className={`bulletin-message${replies === null ? " is-reply" : ""}`}
        key={item.post_id}
        id={`bulletin-${item.post_id}`}
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
          </header>
          {parent && (
            <p className="bulletin-quote">
              <CornerUpLeft size={12} /> {parent.author}: {parent.excerpt}
            </p>
          )}
          <div className="bulletin-text">{item.text}</div>
          {item.refs.length > 0 && (
            <div className="bulletin-refs">
              {item.refs.map((ref) => (
                <button key={ref} type="button" onClick={() => onOpenNode?.(ref)}>
                  {ref}
                </button>
              ))}
            </div>
          )}
          {replies && replies.length > 0 && (
            <button
              type="button"
              className="bulletin-thread-toggle"
              aria-expanded={open}
              onClick={() => toggleThread(item.post_id)}
            >
              {replies.length} {replies.length === 1 ? "reply" : "replies"}
            </button>
          )}
          {replies && open && (
            <div className="bulletin-thread">
              {[...replies].reverse().map((child) => renderPost(child, null))}
            </div>
          )}
        </div>
        {replies && canPost && (
          <button
            type="button"
            className="bulletin-message-action"
            aria-label={`Reply to ${author}`}
            onClick={() => startReply(item)}
          >
            <CornerUpLeft size={15} />
          </button>
        )}
      </article>
    );
  };

  return (
    <section
      className={`bulletin-panel${error || page.error ? " has-error" : ""}`}
      aria-label="Bulletin"
    >
      <header className="bulletin-head">
        <h2>Bulletin</h2>
        {Boolean(error || page.error) && (
          <button
            type="button"
            className="bulletin-icon"
            aria-label={error ? "Retry post" : "Retry refresh"}
            title={error ?? String(page.error)}
            onClick={() => {
              if (error) void post();
              else void reload();
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
                {actor.instruction_title || kinds[actor.kind] || actor.kind}
              </span>
              <span className="bulletin-roster-kind">{kinds[actor.kind] || actor.kind}</span>
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
            void post();
          }}
        >
          {reply && (
            <div className="bulletin-replying">
              <CornerUpLeft size={13} />
              <span>{bulletinAuthor(reply)}</span>
              <span className="bulletin-replying-text">{reply.text}</span>
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
          <textarea
            ref={input}
            rows={1}
            aria-label="Bulletin post"
            placeholder={reply ? "Reply…" : "Message the bulletin"}
            value={draft}
            disabled={busy}
            onChange={(event) => {
              setDraft(event.target.value);
              setKey(crypto.randomUUID());
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                void post();
              }
            }}
          />
          <div className="bulletin-composer-bar">
            <button
              type="button"
              className="bulletin-icon"
              aria-label="Link nodes"
              aria-pressed={refsOpen}
              onClick={() => setRefsOpen((value) => !value)}
            >
              <Link2 size={15} />
            </button>
            {refsOpen && (
              <input
                aria-label="Node references, comma separated"
                placeholder="exp/a, hyp/b"
                value={refs}
                disabled={busy}
                onChange={(event) => {
                  setRefs(event.target.value);
                  setKey(crypto.randomUUID());
                }}
              />
            )}
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
    </section>
  );
}
