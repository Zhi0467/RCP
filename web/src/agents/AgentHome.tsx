import { ArrowLeft, LayoutGrid, LoaderCircle, PanelLeft, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";
import { isChatListToggleShortcut } from "../chat/chatLayout";
import { readPref, writePref } from "../core/prefStorage";
import type { AgentThread } from "../core/types";
import { useAgentThreads } from "./useAgentThreads";
import { useNarrowViewport } from "../ui/useNarrowViewport";
import type { VoiceAgent } from "../voice/useVoiceAgent";
import { AgentThreadView } from "./AgentThreadView";
import {
  agentThreadHash,
  chosenThread,
  railThreads,
  threadIsLive,
  threadTimeLabel,
} from "./agentThreadModel";

const RAIL_FOLDED_KEY = "rcp:agent-rail-collapsed";

function RailRow({
  thread,
  active,
  now,
  onOpen,
}: {
  thread: AgentThread;
  active: boolean;
  now: number;
  onOpen: () => void;
}) {
  const subtitle = thread.kind === "space" ? "Space" : (thread.project_name ?? "");
  return (
    <div className="agent-row thread-row">
      <button
        type="button"
        role="option"
        aria-selected={active}
        aria-current={active ? "page" : undefined}
        aria-label={`${thread.agent_name}, ${subtitle}${thread.unread ? ", unread" : ""}${thread.live_state === "needs_you" ? ", needs you" : ""}`}
        className={`${active ? "active" : ""}${thread.unread ? " has-unread" : ""}`}
        data-state={thread.live_state}
        onClick={onOpen}
      >
        <span className="thread-row-icon" aria-hidden="true">
          {thread.kind === "space" ? (
            <LayoutGrid size={15} />
          ) : threadIsLive(thread) ? (
            <LoaderCircle className="spin" size={14} />
          ) : thread.live_state === "needs_you" ? (
            <span className="needs-you-dot" />
          ) : thread.unread ? (
            <span className="unread-dot" />
          ) : null}
        </span>
        <span className="agent-row-body">
          <span className="agent-row-title">{thread.agent_name}</span>
          <span className="agent-row-meta">{subtitle}</span>
        </span>
        <time className={threadIsLive(thread) ? "live" : undefined}>
          {threadTimeLabel(thread, now)}
        </time>
      </button>
    </div>
  );
}

/**
 * The space home's Agent view: a foldable rail with the Space thread and each
 * project thread, and the open thread beside it. A phone shows one at a time.
 */
export function AgentHome({
  threadId,
  actorId,
  voice,
}: {
  /** The thread the address names, or null for the default. */
  threadId: string | null;
  actorId: string | null;
  voice: VoiceAgent;
}) {
  const narrow = useNarrowViewport();
  const { threads, revision, error, patch } = useAgentThreads();
  const [folded, setFolded] = useState(() => readPref(RAIL_FOLDED_KEY) === "true");
  useEffect(() => writePref(RAIL_FOLDED_KEY, String(folded)), [folded]);
  useEffect(() => {
    const toggle = (event: KeyboardEvent) => {
      if (narrow || !isChatListToggleShortcut(event)) return;
      event.preventDefault();
      setFolded((current) => !current);
    };
    window.addEventListener("keydown", toggle);
    return () => window.removeEventListener("keydown", toggle);
  }, [narrow]);
  const rail = threads ? railThreads(threads) : [];
  // A phone opens a thread only when the address names one; a desktop always shows one.
  const open = narrow && !threadId ? null : chosenThread(rail, threadId);
  const now = Date.now();
  const showRail = narrow ? open === null : !folded;
  return (
    <section
      className={`space-agent${showRail ? "" : " rail-folded"}${open ? " has-thread" : ""}`}
      aria-label="Agent"
    >
      <aside
        className="conversation-list"
        aria-label="Agent threads"
        id="agent-rail-panel"
        hidden={!showRail}
      >
        {!narrow && (
          <div className="agent-list-tools">
            <div className="agent-list-top">
              <button
                aria-controls="agent-rail-panel"
                aria-expanded
                aria-keyshortcuts="Meta+B"
                aria-label="Collapse agent list"
                className="conversation-list-fold"
                onClick={() => setFolded(true)}
                title="Collapse agent list"
                type="button"
              >
                <PanelLeft size={16} />
              </button>
            </div>
          </div>
        )}
        {error && !threads && (
          <p className="agent-list-error" role="alert">
            <TriangleAlert size={14} /> {error}
          </p>
        )}
        {!threads && !error && (
          <p className="agent-rail-loading" role="status">
            <LoaderCircle className="spin" size={14} />
          </p>
        )}
        <div role="listbox" aria-label="Threads">
          {rail.map((thread, index) => (
            <div key={thread.thread_id}>
              <RailRow
                thread={thread}
                active={thread.thread_id === open?.thread_id}
                now={now}
                onOpen={() => (window.location.hash = agentThreadHash(thread.thread_id))}
              />
              {thread.kind === "space" && index < rail.length - 1 && (
                <div className="thread-rail-divider" aria-hidden="true" />
              )}
            </div>
          ))}
        </div>
      </aside>
      {open && (
        <div className="conversation-surface">
          <AgentThreadView
            thread={open}
            actorId={actorId}
            revision={revision}
            voice={voice}
            onThreadChanged={patch}
            leading={
              narrow ? (
                <button
                  className="icon-button thread-back"
                  type="button"
                  aria-label="Threads"
                  onClick={() => (window.location.hash = agentThreadHash(null))}
                >
                  <ArrowLeft size={18} />
                </button>
              ) : folded ? (
                <button
                  aria-controls="agent-rail-panel"
                  aria-expanded={false}
                  aria-keyshortcuts="Meta+B"
                  aria-label="Expand agent list"
                  className="conversation-list-fold"
                  onClick={() => setFolded(false)}
                  title="Expand agent list"
                  type="button"
                >
                  <PanelLeft size={16} />
                </button>
              ) : null
            }
          />
        </div>
      )}
    </section>
  );
}
