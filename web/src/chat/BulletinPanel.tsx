import { useState } from "react";
import { CornerUpLeft, RotateCw, Send, X } from "lucide-react";
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
  const [reply, setReply] = useState<BulletinPost | null>(null);
  const [key, setKey] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
  const resourceId = resourceKeyId(profile.key);
  const reload = () => store.invalidate(resourceId);
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
      setDraft("");
      setRefs("");
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
  const renderPost = (item: BulletinPost) => {
    const parent = bulletinReplyContext(item, page.value?.posts ?? []);
    return (
      <article className="bulletin-post" key={item.post_id} id={`bulletin-${item.post_id}`}>
        <header>
          <strong>{bulletinAuthor(item)}</strong>
          <time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time>
          {!item.reply_to && canPost && (
            <button
              className="icon-button"
              aria-label="Reply"
              onClick={() => {
                setReply(item);
                setKey(crypto.randomUUID());
              }}
            >
              <CornerUpLeft size={15} />
            </button>
          )}
        </header>
        {item.reply_to && parent && (
          <div className="bulletin-parent">
            <span>↳ {parent.author}</span>
            <span className="bulletin-parent-excerpt">{parent.excerpt}</span>
          </div>
        )}
        {item.reply_to && !parent && <span>↳ Earlier post</span>}
        <div className="bulletin-body">{item.text}</div>
        <div className="bulletin-refs">
          {item.refs.map((ref) => (
            <button key={ref} type="button" onClick={() => onOpenNode?.(ref)}>
              {ref}
            </button>
          ))}
        </div>
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
            className="icon-button"
            aria-label={error ? "Retry post" : "Retry refresh"}
            title={error ?? String(page.error)}
            onClick={() => {
              if (error) void post();
              else void reload();
            }}
          >
            <RotateCw size={16} />
          </button>
        )}
      </header>
      <details className="bulletin-roster">
        <summary>
          Agents · {page.value?.roster.length ?? 0}
          {Boolean(page.value?.roster_omitted) && ` · ${page.value!.roster_omitted} omitted`}
        </summary>
        <ul>
          {page.value?.roster.map((actor) => (
            <li key={actor.id}>
              <span title={actor.id}>
                {actor.instruction_title || kinds[actor.kind] || actor.kind}
              </span>
              <span>{kinds[actor.kind] || actor.kind}</span>
              {actor.seat_node_id && (
                <button type="button" onClick={() => onOpenNode?.(actor.seat_node_id!)}>
                  {actor.seat_node_id}
                </button>
              )}
              <span>{actor.state.replaceAll("_", " ")}</span>
              {actor.last_post_at && (
                <time dateTime={actor.last_post_at}>
                  {new Date(actor.last_post_at).toLocaleString()}
                </time>
              )}
            </li>
          ))}
        </ul>
      </details>
      {canPost && (
        <form
          className="bulletin-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void post();
          }}
        >
          {reply && (
            <div className="bulletin-reply">
              <span className="bulletin-parent">
                <span>↳ {bulletinAuthor(reply)}</span>
                <span className="bulletin-parent-excerpt">{reply.text}</span>
              </span>
              <button
                type="button"
                className="icon-button"
                aria-label="Cancel reply"
                onClick={() => {
                  setReply(null);
                  setKey(crypto.randomUUID());
                }}
              >
                <X size={14} />
              </button>
            </div>
          )}
          <textarea
            aria-label="Bulletin post"
            value={draft}
            disabled={busy}
            onChange={(event) => {
              setDraft(event.target.value);
              setKey(crypto.randomUUID());
            }}
          />
          <div className="bulletin-compose-actions">
            <label>
              Node refs
              <input
                aria-label="Node references, comma separated"
                value={refs}
                disabled={busy}
                onChange={(event) => {
                  setRefs(event.target.value);
                  setKey(crypto.randomUUID());
                }}
              />
            </label>
            <button
              className="icon-button"
              type="submit"
              aria-label="Post"
              disabled={busy || !draft.trim()}
            >
              <Send size={16} />
            </button>
          </div>
        </form>
      )}
      <div className="bulletin-posts">
        {bulletinThreads(page.value?.posts ?? []).map(({ post: item, replies }) => (
          <div className="bulletin-thread" key={item.post_id}>
            {renderPost(item)}
            <div className="bulletin-replies">{replies.map(renderPost)}</div>
          </div>
        ))}
      </div>
      <footer>
        {cursor && (
          <button type="button" onClick={() => setCursor(null)}>
            Newest
          </button>
        )}
        {page.value?.next_cursor && (
          <button type="button" onClick={() => setCursor(page.value!.next_cursor)}>
            Older
          </button>
        )}
      </footer>
    </section>
  );
}
