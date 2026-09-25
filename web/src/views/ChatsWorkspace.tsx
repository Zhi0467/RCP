import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Circle,
  LoaderCircle,
  MessageCircle,
  PauseCircle,
  Search,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  CONVERSATION_AGENT_GROUPS,
  conversationAgentStatus,
  groupConversationAgents,
  type ChatConversation,
  type ConversationAgentGroup,
  type ConversationAgentState,
  type ConversationAgentStatus,
} from "../chatWorkspace";
import type { GlossaryIndex } from "../glossary";
import {
  CHAT_LIST_DEFAULT_WIDTH,
  CHAT_LIST_COLLAPSED_WIDTH,
  CHAT_LIST_DIVIDER_WIDTH,
  CHAT_LIST_MIN_WIDTH_COMPACT,
  chatListWidthBounds,
  clampChatListWidth,
  isChatListToggleShortcut,
  type ChatListWidthBounds,
} from "../chatLayout";
import type {
  AgentTask,
  ChatTranscript,
  GraphNode,
  ProjectSnapshot,
  StartAgentTask,
  WatcherRecord,
} from "../types";
import { NodeChat } from "../components/NodeChat";
import { useNarrowViewport } from "../hooks/useNarrowViewport";

interface Props {
  project: ProjectSnapshot;
  conversations: ChatConversation[];
  selectedChatId: string | null;
  nodes: Record<string, GraphNode>;
  glossaryIndex: GlossaryIndex;
  runScope: string[];
  tasks: AgentTask[];
  watchers: WatcherRecord[];
  graphChangesDisabled: boolean;
  unreadTaskIds: ReadonlySet<string>;
  chatTranscripts: ReadonlyMap<string, ChatTranscript>;
  hasMore: boolean;
  loadingMore: boolean;
  onSelect: (chatId: string) => void;
  onLoadMore: () => void;
  onStartTask: StartAgentTask;
  onResumeTask: (task: AgentTask) => void;
  onRetryTask: (task: AgentTask) => void;
  onRefreshTask: (taskId: string) => Promise<AgentTask>;
  onInspectTask: (taskId: string) => void;
  onOpenInbox: () => void;
  onOpenNode?: (nodeId: string) => void;
  onRepairGraphUpdate: (taskId: string) => Promise<void>;
  onStopWatcher?: (watcherId: string) => void;
  onNewSession: (conversation: ChatConversation) => void;
}

function chatListWidthStorageKey(projectId: string): string {
  return `rcp:chat-list-width:${projectId}`;
}

function chatListCollapsedStorageKey(projectId: string): string {
  return `rcp:chat-list-collapsed:${projectId}`;
}

function readChatListWidth(projectId: string): number {
  if (typeof window === "undefined") return CHAT_LIST_DEFAULT_WIDTH;
  try {
    const value = Number(window.localStorage.getItem(chatListWidthStorageKey(projectId)));
    return Number.isFinite(value) && value >= CHAT_LIST_MIN_WIDTH_COMPACT
      ? value
      : CHAT_LIST_DEFAULT_WIDTH;
  } catch {
    return CHAT_LIST_DEFAULT_WIDTH;
  }
}

function readChatListCollapsed(projectId: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(chatListCollapsedStorageKey(projectId)) === "true";
  } catch {
    return false;
  }
}

type AgentFilter = "all" | "needs_you" | "working";

const GROUP_LABELS: Record<ConversationAgentGroup, string> = {
  needs_you: "Needs you",
  working: "Working",
  recent: "Recent",
};

/** A needs-you or paused row shows the backend's status label as its reason. */
function needsHuman(status: ConversationAgentStatus): boolean {
  return status.group === "needs_you";
}

function AgentStateIcon({ state }: { state: ConversationAgentState }) {
  if (state === "needs_you") return <AlertCircle size={15} aria-hidden="true" />;
  if (state === "paused") return <PauseCircle size={15} aria-hidden="true" />;
  if (state === "working") return <LoaderCircle className="spin" size={15} aria-hidden="true" />;
  if (state === "unread") return <CheckCircle2 size={15} aria-hidden="true" />;
  return <Circle size={15} aria-hidden="true" />;
}

function sinceLabel(timestamp: string | null | undefined, now: number): string {
  const at = timestamp ? Date.parse(timestamp) : Number.NaN;
  if (!Number.isFinite(at)) return "";
  const minutes = Math.max(0, Math.round((now - at) / 60_000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 24 * 60) return `${Math.round(minutes / 60)}h`;
  return new Date(at).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function modeLabel(status: ConversationAgentStatus): string | null {
  const mode = status.latest?.request.mode;
  return mode === "work" ? "Work" : mode === "discuss" ? "Discuss" : null;
}

function agentMeta(status: ConversationAgentStatus): string {
  const latest = status.latest;
  if (!latest) return "";
  const parts: (string | null | undefined)[] = [latest.runtime_label, modeLabel(status)];
  if (status.state === "working") {
    parts.push(latest.phase, `${Math.max(1, Math.round(latest.elapsed_seconds / 60))}m`);
  } else {
    parts.push(latest.request.run_truth_scope?.join(", "));
  }
  return parts.filter(Boolean).join(" · ");
}

export function ChatsWorkspace({
  project,
  conversations,
  selectedChatId,
  nodes,
  glossaryIndex,
  runScope,
  tasks,
  watchers,
  graphChangesDisabled,
  unreadTaskIds,
  chatTranscripts,
  hasMore,
  loadingMore,
  onSelect,
  onLoadMore,
  onStartTask,
  onResumeTask,
  onRetryTask,
  onRefreshTask,
  onInspectTask,
  onOpenInbox,
  onOpenNode,
  onRepairGraphUpdate,
  onStopWatcher,
  onNewSession,
}: Props) {
  const narrow = useNarrowViewport();
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const [filter, setFilter] = useState<AgentFilter>("all");
  const [query, setQuery] = useState("");
  const [listWidth, setListWidth] = useState(() => readChatListWidth(project.id));
  const [listCollapsed, setListCollapsed] = useState(() => readChatListCollapsed(project.id));
  const [widthBounds, setWidthBounds] = useState<ChatListWidthBounds>(() =>
    chatListWidthBounds(typeof window === "undefined" ? 1200 : window.innerWidth),
  );
  const workspace = useRef<HTMLElement>(null);
  const groups = groupConversationAgents(conversations, unreadTaskIds, query);
  const visibleGroups = CONVERSATION_AGENT_GROUPS.filter(
    (group) => filter === "all" || group === filter,
  );
  const now = Date.now();
  const selected =
    conversations.find((conversation) => conversation.chatId === selectedChatId) ??
    conversations[0] ??
    null;

  useEffect(() => {
    setListWidth(readChatListWidth(project.id));
    setListCollapsed(readChatListCollapsed(project.id));
  }, [project.id]);

  useEffect(() => {
    if (!project.id) return;
    try {
      window.localStorage.setItem(chatListWidthStorageKey(project.id), String(listWidth));
    } catch {
      // Layout state is a convenience; storage failures must not affect chat.
    }
  }, [listWidth, project.id]);

  useEffect(() => {
    if (!project.id) return;
    try {
      window.localStorage.setItem(chatListCollapsedStorageKey(project.id), String(listCollapsed));
    } catch {
      // Layout state is a convenience; storage failures must not affect chat.
    }
  }, [listCollapsed, project.id]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isChatListToggleShortcut(event)) return;
      event.preventDefault();
      if (narrow) setMobileListOpen((current) => !current);
      else setListCollapsed((current) => !current);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [narrow]);

  useEffect(() => {
    const element = workspace.current;
    if (!element || narrow) return;

    const updateBounds = (width: number) => {
      const nextBounds = chatListWidthBounds(width);
      setWidthBounds(nextBounds);
      setListWidth((current) => clampChatListWidth(current, nextBounds));
    };
    updateBounds(element.getBoundingClientRect().width);
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) updateBounds(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [project.id, narrow]);

  const selectedStatus = selected ? conversationAgentStatus(selected, unreadTaskIds) : null;
  const selectedLatest = selectedStatus?.latest ?? null;
  const resizeFromPointer = (clientX: number) => {
    const bounds = workspace.current?.getBoundingClientRect();
    if (!bounds) return;
    setListWidth(clampChatListWidth(clientX - bounds.left, widthBounds));
  };

  return (
    <section
      className="chats-workspace"
      ref={workspace}
      style={{
        gridTemplateColumns: narrow
          ? undefined
          : `${listCollapsed ? CHAT_LIST_COLLAPSED_WIDTH : listWidth}px ${CHAT_LIST_DIVIDER_WIDTH}px minmax(0, 1fr)`,
      }}
    >
      <button
        className="conversation-list-toggle view-disclosure-summary"
        type="button"
        aria-controls="conversation-list-panel"
        aria-expanded={mobileListOpen}
        onClick={() => setMobileListOpen((current) => !current)}
      >
        <MessageCircle size={14} />
        <span>Agents</span>
        <ChevronDown size={14} />
      </button>
      <aside
        className="conversation-list"
        aria-label="Project conversations"
        hidden={narrow ? !mobileListOpen : listCollapsed}
        id="conversation-list-panel"
      >
        <header>
          <MessageCircle size={16} />
          <strong>Agents</strong>
        </header>
        <div className="agent-list-tools">
          <label className="agent-list-search">
            <Search size={13} aria-hidden="true" />
            <input
              type="search"
              aria-label="Search agents"
              placeholder="Search agents"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div className="agent-list-filters" role="group" aria-label="Filter agents">
            {(["all", "needs_you", "working"] as const).map((value) => {
              const count =
                value === "all"
                  ? groups.needs_you.length + groups.working.length + groups.recent.length
                  : groups[value].length;
              return (
                <button
                  type="button"
                  key={value}
                  data-filter={value}
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {value === "all" ? "All" : GROUP_LABELS[value]} <span>{count}</span>
                </button>
              );
            })}
          </div>
        </div>
        <div role="listbox" aria-label="Conversations">
          {visibleGroups.map((group) =>
            groups[group].length === 0 ? null : (
              <div
                className="agent-group"
                role="group"
                aria-label={GROUP_LABELS[group]}
                data-group={group}
                key={group}
              >
                <div className="agent-group-heading" aria-hidden="true">
                  <span>{GROUP_LABELS[group]}</span>
                  <span>{groups[group].length}</span>
                </div>
                {groups[group].map(({ conversation, status }) => {
                  const selectedConversation = conversation.chatId === selected?.chatId;
                  const unread = status.state === "unread";
                  const latest = status.latest;
                  return (
                    <button
                      type="button"
                      role="option"
                      aria-selected={selectedConversation}
                      aria-current={selectedConversation ? "page" : undefined}
                      aria-label={`${conversation.title}, ${conversation.kind === "project_chat" ? "project" : "node"} conversation${unread ? ", unread result" : ""}`}
                      className={`${selectedConversation ? "active" : ""}${unread ? " unread" : ""}`}
                      data-state={status.state}
                      title={conversation.title}
                      onClick={() => {
                        onSelect(conversation.chatId);
                        if (narrow) setMobileListOpen(false);
                      }}
                      key={conversation.chatId}
                    >
                      <span className="agent-row-icon">
                        <AgentStateIcon state={status.state} />
                      </span>
                      <span className="agent-row-body">
                        <span className="agent-row-title">{conversation.title}</span>
                        {needsHuman(status) && latest && (
                          <span className="agent-row-reason">{latest.status_label}</span>
                        )}
                        {latest && <span className="agent-row-meta">{agentMeta(status)}</span>}
                      </span>
                      <time>
                        {status.state === "working"
                          ? "live"
                          : sinceLabel(
                              latest?.last_activity_at ?? conversation.updatedAt ?? null,
                              now,
                            )}
                      </time>
                    </button>
                  );
                })}
              </div>
            ),
          )}
        </div>
        {hasMore && (
          <footer className="conversation-list-more">
            <button
              className="button primary compact"
              type="button"
              disabled={loadingMore}
              onClick={onLoadMore}
            >
              {loadingMore && <LoaderCircle className="spin" size={12} />}
              Load more
            </button>
          </footer>
        )}
      </aside>
      <div className={`conversation-divider${listCollapsed ? " is-collapsed" : ""}`}>
        <div
          aria-controls="conversation-list-panel conversation-surface-panel"
          aria-hidden={listCollapsed}
          aria-label="Resize conversation list"
          aria-orientation="vertical"
          aria-valuemax={widthBounds.maximum}
          aria-valuemin={widthBounds.minimum}
          aria-valuenow={Math.round(listWidth)}
          className="conversation-resize-handle"
          onKeyDown={
            listCollapsed
              ? undefined
              : (event) => {
                  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                    event.preventDefault();
                    setListWidth((current) =>
                      clampChatListWidth(
                        current + (event.key === "ArrowLeft" ? -16 : 16),
                        widthBounds,
                      ),
                    );
                  }
                  if (event.key === "Home" || event.key === "End") {
                    event.preventDefault();
                    setListWidth(event.key === "Home" ? widthBounds.minimum : widthBounds.maximum);
                  }
                }
          }
          onPointerCancel={
            listCollapsed
              ? undefined
              : (event) => {
                  if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                    event.currentTarget.releasePointerCapture(event.pointerId);
                  }
                }
          }
          onPointerDown={
            listCollapsed
              ? undefined
              : (event) => {
                  event.currentTarget.setPointerCapture(event.pointerId);
                  resizeFromPointer(event.clientX);
                }
          }
          onPointerMove={
            listCollapsed
              ? undefined
              : (event) => {
                  if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                    resizeFromPointer(event.clientX);
                  }
                }
          }
          onPointerUp={
            listCollapsed
              ? undefined
              : (event) => {
                  if (event.currentTarget.hasPointerCapture(event.pointerId)) {
                    event.currentTarget.releasePointerCapture(event.pointerId);
                  }
                }
          }
          role="separator"
          tabIndex={listCollapsed ? -1 : 0}
        />
        <button
          aria-controls="conversation-list-panel"
          aria-expanded={!listCollapsed}
          aria-keyshortcuts="Meta+B"
          aria-label={listCollapsed ? "Expand conversation list" : "Collapse conversation list"}
          className="conversation-divider-toggle"
          onClick={() => setListCollapsed((current) => !current)}
          type="button"
        >
          {listCollapsed ? <ChevronRight size={13} /> : <ChevronLeft size={13} />}
        </button>
      </div>
      <div className="conversation-surface" id="conversation-surface-panel">
        {selected && selectedStatus && (
          <header className="conversation-header" data-state={selectedStatus.state}>
            <strong>{selected.title}</strong>
            <span className="conversation-header-meta">
              {[
                selectedLatest?.runtime_label,
                selectedLatest?.request.model,
                modeLabel(selectedStatus),
                selectedLatest?.request.run_truth_scope?.join(", "),
                selected.kind === "project_chat" ? "Project chat" : "Node chat",
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
            {needsHuman(selectedStatus) && selectedLatest && (
              <div className="conversation-header-banner" role="status">
                <span>{selectedLatest.status_label}</span>
                {selectedLatest.can_resume && (
                  <button
                    className="button compact"
                    type="button"
                    onClick={() => onResumeTask(selectedLatest)}
                  >
                    Resume
                  </button>
                )}
                {!selectedLatest.can_resume && selectedLatest.can_retry && (
                  <button
                    className="button compact"
                    type="button"
                    onClick={() => onRetryTask(selectedLatest)}
                  >
                    Retry
                  </button>
                )}
              </div>
            )}
          </header>
        )}
        {selected ? (
          <NodeChat
            key={selected.chatId}
            project={project}
            node={selected.nodeId ? (nodes[selected.nodeId] ?? null) : null}
            nodes={nodes}
            glossaryIndex={glossaryIndex}
            conversationTitle={selected.kind === "node_chat" ? selected.title : undefined}
            runScope={runScope}
            tasks={tasks}
            watchers={watchers}
            historyMessages={chatTranscripts.get(selected.chatId)?.messages}
            chatId={selected.chatId}
            presentation="workspace"
            graphChangesDisabled={graphChangesDisabled}
            onStartTask={onStartTask}
            onResumeTask={onResumeTask}
            onRetryTask={onRetryTask}
            onRefreshTask={onRefreshTask}
            onInspectTask={onInspectTask}
            onOpenInbox={onOpenInbox}
            onRepairGraphUpdate={onRepairGraphUpdate}
            onOpenNode={onOpenNode}
            onStopWatcher={onStopWatcher}
            onNewSession={() => onNewSession(selected)}
            onClose={() => undefined}
          />
        ) : null}
      </div>
    </section>
  );
}
